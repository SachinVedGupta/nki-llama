# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch LLaMA model for NXD inference."""
import copy
import gc
import logging
import math
from typing import List, Optional, Tuple, Type

logger = logging.getLogger("Neuron")

import torch
from torch import nn
from neuronx_distributed.parallel_layers import parallel_state  # noqa: E402
from neuronx_distributed.parallel_layers.layers import (  # noqa: E402; noqa: E402; noqa: E402; noqa: E402; noqa: E402
    ColumnParallelLinear,
    ParallelEmbedding,
    RowParallelLinear,
)
from neuronx_distributed.parallel_layers.mappings import (
    gather_from_sequence_parallel_region,
    reduce_from_tensor_model_parallel_region,
    reduce_scatter_to_sequence_parallel_region,
)
from neuronx_distributed.parallel_layers.utils import get_padding_length
from neuronx_distributed.quantization.quantization_config import QuantizationType, QuantizedDtype
from neuronx_distributed.quantization.quantization_layers import (  # noqa: E402; noqa: E402; noqa: E402; noqa: E402; noqa: E402
    QuantizedColumnParallel,
    QuantizedRowParallel,
)
from neuronxcc.nki._private_kernels.mlp import (
    mlp_fused_add_isa_kernel,
    mlp_isa_kernel,
    quant_mlp_fused_add_isa_kernel,
    quant_mlp_isa_kernel,
)
from neuronxcc.nki._private_kernels.rmsnorm import rmsnorm_quant_isa_kernel
from neuronxcc.starfish.penguin.targets.nki.private_api import vnc
from torch import nn, ones
from torch_neuronx.xla_impl.ops import nki_jit
from transformers import LlamaForCausalLM
from transformers.activations import ACT2FN
from transformers.models.llama.modeling_llama import LlamaRMSNorm, LlamaRotaryEmbedding

from neuronx_distributed_inference.models.config import InferenceConfig, NeuronConfig  # noqa: E402
from neuronx_distributed_inference.models.model_base import (  # noqa: E402
    NeuronBaseForCausalLM,
    NeuronBaseModel,
)
from neuronx_distributed_inference.modules.attention.attention_base import NeuronAttentionBase
from neuronx_distributed_inference.modules.attention.gqa import (  # noqa: E402
    BaseGroupQueryAttention,
)
from neuronx_distributed_inference.modules.attention.utils import (
    RotaryEmbedding,
    preprocess_quantized_linear_layer,
    transpose_parallel_linear_layer,
)

# from neuronx_distributed_inference.modules.custom_calls import CustomRMSNorm
from neuronx_distributed_inference.modules.flashdecode.utils import calculate_num_cores_per_group
from neuronx_distributed_inference.modules.lora_serving.lora_module import is_lora_module
from neuronx_distributed_inference.utils.distributed import get_tp_group

from torch_neuronx.xla_impl.ops import RmsNorm

import neuronxcc.nki as nki
import neuronxcc.nki.language as nl

SIMPLE_PROFILE = False
_LLAMA_MODULE_MAP = {}


# logger = logging.getLogger("Neuron")
# logger.setLevel(level=logging.DEBUG)
# handler = logging.FileHandler("./app.log", encoding='UTF-8')
# handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# handler.setFormatter(formatter)
# logger.addHandler(handler)


# Optimized NKI matrix multiplication compared to torch.matmul
# refer to https://awsdocs-neuron.readthedocs-hosted.com/en/latest/general/nki/tutorials/matrix_multiplication.html
# for small input matrices/tensors
@nki.jit 
def matmul_nki(lhs, rhs, size_lhs, size_rhs):
    nki_lhs = nl.load(lhs[:, :])
    nki_rhs = nl.load(rhs[:, :])

    result_psum = nl.zeros((size_lhs, size_rhs), nl.float32, buffer=nl.psum)
    result_psum = nl.matmul(nki_lhs, nki_rhs, transpose_x=True)

    result = nl.ndarray((size_lhs, size_rhs), dtype=lhs.dtype, buffer=nl.shared_hbm)
    nl.store(result[:, :], value=result_psum)
    return result


# Optimized multiplication of large matrices using block matrix multiplication and NKI matmul
def block_matmul_nki(A, B, block_size=512, block_size_1=128):
    m, k = A.shape
    k, n = B.shape

    # create empty result tensor/matrix
    result = torch.zeros((m, n), dtype = A.dtype, device=A.device)

    for i in range(0, m, block_size_1):
        for j in range(0, n, block_size):
            for l in range(0, k, block_size_1):
                A_block = A[i:i+block_size_1, l:l+block_size_1]
                B_block = B[l:l+block_size_1, j:j+block_size]
                m1, k1 = A_block.shape
                k1, n1 = B_block.shape

                # apply matmul_nki to each (small) block
                result[i:i+block_size_1, j:j+block_size] += matmul_nki(A_block.T, B_block, m1, n1)
    
    # using torch.matmul instead of nki.matmul causes NKI FLOPS to go to 0
    # result = result.to(original_dtype)
    return result 


@nki.jit
def nki_rmsnorm_kernel(a_tensor, g_tensor, eps):
    # Calculate out_tensor = a_tensor/RMS(a_tensor) * g_tensor
    # Where RMS(a_tensor) = sqrt((1/N) * sum(a_tensor * a_tensor))
    # and N = a_tensor.shape[1]
    # Reduction (mean) is performed in the free (2nd) dimension
    out_tensor = nl.ndarray(a_tensor.shape, dtype=a_tensor.dtype,
                          buffer=nl.shared_hbm)

    # Make sure shapes match
    assert a_tensor.shape[2] == g_tensor.shape[0]

    # Generate tensor indices to index input tensor
    ix = nl.arange(128)[:, None]
    iw = nl.arange(1)[:, None]
    iy = nl.arange(a_tensor.shape[2])[None, :]

    num_rows = a_tensor.shape[1]

    # Load RMSNorm weight once, reused by rows/tiles of a_tensor
    g_tile = nl.load(g_tensor.reshape((1, g_tensor.shape[0]))[iw, iy])

    # Process 128 rows at a time due to 128-partition tile size limitation
    # Since we're not reducing across the first dimension
    # Tiles can be processed independently

    for b in range(a_tensor.shape[0]):
        for i in range(math.ceil(a_tensor.shape[1]/128)):
            # Load input data from external memory to on-chip memory
            a_tile = nl.zeros([128, a_tensor.shape[2]], a_tensor.dtype)
            a_tile[...] = nl.load(a_tensor[b, i * 128 + ix, iy], mask=(i * 128 + ix < num_rows))

            # Compute element-wise square of a_tensor
            in_square = nl.square(a_tile)

            # Calculate sum of squared elements, along last dimension
            square_sum = nl.sum(in_square, axis=[1])

            # Scale and get a reciprocal
            mean = square_sum / a_tensor.shape[2]

            # Take square root of mean and then reciprocal with
            # rsqrt API (one ISA instruction)
            rms_reciprocal = nl.rsqrt(mean + eps)

            # Scale the input tensor
            out_tile = nl.multiply(a_tile, rms_reciprocal)

            # Broadcast weight along first axis to match tensor shape
            # num_rows_active = min(num_rows - i * 128, 128)
            g_bcast = g_tile.broadcast_to((128, g_tensor.shape[0]))

            # Multiply with the RMSNorm weight
            out_tile[...] = nl.multiply(out_tile, g_bcast, mask=(i * 128 + ix < num_rows))

            # store the addition results back to external memory (out_tensor)
            nl.store(out_tensor[b, i * 128 + ix, iy], value=out_tile, mask=(i * 128 + ix < num_rows))

    return out_tensor


class CustomRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6, nki_enabled=False):
        """
        Use this RMSNorm to perform customized rmsnorm on Neuron
        Note: CustomRMSNorm forward method calls target="AwsNeuronRmsNorm"
        """
        super().__init__()
        self.weight = nn.Parameter(ones(hidden_size))
        self.variance_epsilon = eps
        self.nki_enabled = nki_enabled

    def forward(self, hidden_states):
        if self.nki_enabled:
            out_tensor = nki_rmsnorm_kernel(hidden_states, self.weight, self.variance_epsilon)
            return out_tensor

        original_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        result = RmsNorm.apply(
            hidden_states, self.weight, self.variance_epsilon, len(hidden_states.shape) - 1
        )

        return result.to(original_dtype)


def get_rmsnorm_cls():
    # Initialize to the appropriate implementation of RMSNorm
    # If infer on NXD -> CustomRMSNorm
    # If infer on CPU -> HF_RMSNorm (CustomRMSNorm does not work on CPU)
    return CustomRMSNorm if parallel_state.model_parallel_is_initialized() else LlamaRMSNorm


def preshard_hook_fn(module: torch.nn.Module, model_state_dict: dict, prefix: str) -> bool:
    if isinstance(module, (BaseGroupQueryAttention,)):
        return module.preshard_hook(model_state_dict, prefix)

    return False


def _register_module(key: str, cls: Type[nn.Module]):
    _LLAMA_MODULE_MAP[key] = cls


def register_module(key: str):
    """
    Register a module for use in NeuronLlama.

    Arguments:
        key: String used to identify the module

    Example:
        @register_module("NeuronLlamaAttention")
        class NeuronLlamaAttention(nn.Module):
            ...
    """

    def inner(cls: Type[nn.Module]):
        _register_module(key, cls)
        return cls

    return inner


def convert_state_dict_to_fused_qkv(llama_state_dict, cfg: InferenceConfig):
    """
    This function concats the qkv weights to a Wqkv weight for fusedqkv, and deletes the qkv weights.
    """
    for l in range(cfg.num_hidden_layers):  # noqa: E741
        llama_state_dict[f"layers.{l}.self_attn.Wqkv.weight"] = torch.cat(
            [
                llama_state_dict[f"layers.{l}.self_attn.q_proj.weight"],
                llama_state_dict[f"layers.{l}.self_attn.k_proj.weight"],
                llama_state_dict[f"layers.{l}.self_attn.v_proj.weight"],
            ],
        )
        del llama_state_dict[f"layers.{l}.self_attn.q_proj.weight"]
        del llama_state_dict[f"layers.{l}.self_attn.k_proj.weight"]
        del llama_state_dict[f"layers.{l}.self_attn.v_proj.weight"]

    gc.collect()

    return llama_state_dict


class NeuronConfigNKI(NeuronConfig):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.nki_enabled = kwargs.pop("enable_nki", False)


class LlamaInferenceConfig(InferenceConfig):
    def add_derived_config(self):
        self.num_cores_per_group = 1
        if self.neuron_config.flash_decoding_enabled:
            num_attn_heads, num_kv_heads = self.num_attention_heads, self.num_key_value_heads
            self.num_cores_per_group = calculate_num_cores_per_group(
                num_attn_heads, num_kv_heads, self.neuron_config.tp_degree
            )

    def get_required_attributes(self) -> List[str]:
        return [
            "hidden_size",
            "num_attention_heads",
            "num_hidden_layers",
            "num_key_value_heads",
            "pad_token_id",
            "vocab_size",
            "max_position_embeddings",
            "rope_theta",
            "rms_norm_eps",
            "hidden_act",
        ]

    @classmethod
    def get_neuron_config_cls(cls) -> Type[NeuronConfig]:
        return NeuronConfigNKI


class NeuronLlamaMLP(nn.Module):
    """
    This class just replace the linear layers (gate_proj, up_proj and down_proj) with column and row parallel layers
    """

    def __init__(self, config: InferenceConfig):
        super().__init__()
        self.config = config
        self.neuron_config = config.neuron_config
        self.tp_degree = config.neuron_config.tp_degree
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        self.act_fn = ACT2FN[config.hidden_act]

        self.sequence_parallel_enabled = getattr(
            self.neuron_config, "sequence_parallel_enabled", False
        )
        self.sequence_dimension = 1 if self.sequence_parallel_enabled else None
        self.rms_norm_eps = config.rms_norm_eps
        self.mlp_kernel_enabled = self.neuron_config.mlp_kernel_enabled
        self.quantized_mlp_kernel_enabled = self.neuron_config.quantized_mlp_kernel_enabled
        self.rmsnorm_quantize_kernel_enabled = self.neuron_config.rmsnorm_quantize_kernel_enabled
        self.quantized_kernel_lower_bound = self.neuron_config.quantized_kernel_lower_bound
        self.logical_neuron_cores = self.neuron_config.logical_neuron_cores
        mlp_bias = getattr(config, "mlp_bias", False)
        if parallel_state.model_parallel_is_initialized():
            if self.quantized_mlp_kernel_enabled:
                # Quantized MLP kernels expect intermediate size to be multiple of 128, so we need to pad
                tp_degree = self.neuron_config.tp_degree
                self.intermediate_size += (
                    get_padding_length(self.intermediate_size // tp_degree, 128) * tp_degree
                )
                logger.debug(f"Quantized intermediate_size: {self.intermediate_size}")

                quantization_type = QuantizationType(self.neuron_config.quantization_type)
                quantized_dtype = QuantizedDtype.F8E4M3
                self.gate_proj = QuantizedColumnParallel(
                    input_size=self.hidden_size,
                    output_size=self.intermediate_size,
                    bias=mlp_bias,
                    gather_output=False,
                    sequence_parallel_enabled=False,
                    dtype=config.neuron_config.torch_dtype,
                    quantized_dtype=quantized_dtype,
                    quantization_type=quantization_type,
                    tensor_model_parallel_group=get_tp_group(config),
                )
                self.up_proj = QuantizedColumnParallel(
                    input_size=self.hidden_size,
                    output_size=self.intermediate_size,
                    bias=mlp_bias,
                    gather_output=False,
                    sequence_parallel_enabled=False,
                    dtype=config.neuron_config.torch_dtype,
                    quantized_dtype=quantized_dtype,
                    quantization_type=quantization_type,
                    tensor_model_parallel_group=get_tp_group(config),
                )
                self.down_proj = QuantizedRowParallel(
                    input_size=self.intermediate_size,
                    output_size=self.hidden_size,
                    bias=mlp_bias,
                    quantization_type=quantization_type,
                    input_is_parallel=True,
                    dtype=config.neuron_config.torch_dtype,
                    quantized_dtype=quantized_dtype,
                    sequence_parallel_enabled=False,
                    quantization_per_channel_axis=0,
                    tensor_model_parallel_group=get_tp_group(config),
                )

            else:
                self.gate_proj = ColumnParallelLinear(
                    self.hidden_size,
                    self.intermediate_size,
                    bias=mlp_bias,
                    gather_output=False,
                    dtype=config.neuron_config.torch_dtype,
                    pad=True,
                    sequence_parallel_enabled=False,
                    sequence_dimension=None,
                    tensor_model_parallel_group=get_tp_group(config),
                )
                self.up_proj = ColumnParallelLinear(
                    self.hidden_size,
                    self.intermediate_size,
                    bias=mlp_bias,
                    gather_output=False,
                    dtype=config.neuron_config.torch_dtype,
                    pad=True,
                    sequence_parallel_enabled=False,
                    sequence_dimension=None,
                    tensor_model_parallel_group=get_tp_group(config),
                )
                self.down_proj = RowParallelLinear(
                    self.intermediate_size,
                    self.hidden_size,
                    bias=mlp_bias,
                    input_is_parallel=True,
                    dtype=config.neuron_config.torch_dtype,
                    pad=True,
                    sequence_parallel_enabled=self.sequence_parallel_enabled,
                    sequence_dimension=self.sequence_dimension,
                    tensor_model_parallel_group=get_tp_group(config),
                    reduce_dtype=config.neuron_config.rpl_reduce_dtype,
                )

            if self.mlp_kernel_enabled:
                if self.quantized_mlp_kernel_enabled:
                    preprocess_quantized_linear_layer(self.gate_proj)
                    preprocess_quantized_linear_layer(self.up_proj)
                    preprocess_quantized_linear_layer(self.down_proj)

                else:
                    # Transpose the weights to the layout expected by kernels
                    self.gate_proj.weight = transpose_parallel_linear_layer(self.gate_proj.weight)
                    self.up_proj.weight = transpose_parallel_linear_layer(self.up_proj.weight)
                    self.down_proj.weight = transpose_parallel_linear_layer(self.down_proj.weight)

        else:
            self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=mlp_bias)
            self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=mlp_bias)
            self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=mlp_bias)

    def _kernel_enabled_quantized_mlp(self, x, fused_rmsnorm, rmsnorm, residual, adapter_ids):
        grid = (vnc(self.logical_neuron_cores),)
        fused_residual = residual is not None
        logger.debug(
            f"MLP: quantized kernel, fused_residual={fused_residual}, fused_rmsnorm={fused_rmsnorm}, logical_neuron_cores={self.logical_neuron_cores}"
        )

        # Can't do residual add in the kernel if SP is enabled
        if fused_residual:
            assert (
                not self.sequence_parallel_enabled
            ), "Quantized MLP cannot have both fused residual add and sequence parallel RMSnorm!"
            # Using fused residual add
            _mlp_fwd_call = nki_jit()(quant_mlp_fused_add_isa_kernel)
        else:
            _mlp_fwd_call = nki_jit()(quant_mlp_isa_kernel)

        # Handle SP RMSnorm
        x_orig_dtype = x.dtype
        if self.sequence_parallel_enabled:
            # This RMSNormQuant kernel will do quantization inside, so we pass the
            # lower_bound for clipping.
            # If we don't use this kernel, the MLP kernel below will do the
            # quantization, so we also pass lower_bound to that kernel.
            if self.rmsnorm_quantize_kernel_enabled:
                logger.debug(
                    "Running Quantized MLP kernel with sequence-parallel RMSnorm-Quantize kernel!"
                )
                _rmsnorm_quant_fwd_call = nki_jit()(rmsnorm_quant_isa_kernel)
                quant_rmsnorm_out = torch.zeros(
                    size=(
                        x.shape[0],  # batch size
                        x.shape[1],  # sequence length
                        x.shape[2] + 4,  # hidden size + 4 bytes for packing fp32 scale
                    ),
                    dtype=torch.int8,
                    device=x.device,
                )
                ln_w = rmsnorm.weight.unsqueeze(0)
                lower_bound = self.quantized_kernel_lower_bound
                _rmsnorm_quant_fwd_call[grid](
                    x, ln_w, lower_bound, quant_rmsnorm_out, kernel_name="QuantOnly"
                )
                x = gather_from_sequence_parallel_region(
                    quant_rmsnorm_out,
                    self.sequence_dimension,
                    process_group=get_tp_group(self.config),
                )

            else:
                logger.debug(
                    "Running Quantized MLP kernel with external (native compiler) sequence-parallel RMSnorm!"
                )
                x = gather_from_sequence_parallel_region(
                    x, self.sequence_dimension, process_group=get_tp_group(self.config)
                )

        # Build output tensor
        output_tensor_seqlen = x.shape[1]
        if fused_residual:
            # seqlen dim is doubled to store the residual add output
            output_tensor_seqlen *= 2

        output_tensor = torch.zeros(
            size=(
                x.shape[0],  # batch size
                output_tensor_seqlen,
                self.hidden_size,  # hidden size
            ),
            dtype=x_orig_dtype,
            device=x.device,
        )

        # Grab weights
        # all weights of the layers are stored in (out, in) shape
        # unsqueeze so that shape of RMS gamma weight is [1, hidden] instead of [hidden]
        ln_w = rmsnorm.weight.unsqueeze(0)
        gate_w = self.gate_proj.weight.data
        gate_w_scale = self.gate_proj.weight_scale
        up_w = self.up_proj.weight.data
        up_w_scale = self.up_proj.weight_scale
        down_w = self.down_proj.weight.data
        down_w_scale = self.down_proj.weight_scale
        lower_bound = self.quantized_kernel_lower_bound

        if fused_residual:
            _mlp_fwd_call[grid](
                x,  # attn_output
                residual,  # hidden
                ln_w,  # ln_w
                gate_w,  # gate_w
                gate_w_scale,
                up_w,  # up_w
                up_w_scale,
                down_w,  # down_w
                down_w_scale,
                lower_bound,
                output_tensor,  # out
                fused_rmsnorm=fused_rmsnorm,
                eps=self.rms_norm_eps,
                kernel_name="MLP",
                store_add=True,
            )
            original_seqlen = x.shape[1]
            residual = output_tensor[:, original_seqlen:, :]
            output_tensor = output_tensor[:, :original_seqlen, :]
        else:
            _mlp_fwd_call[grid](
                x,  # hidden
                # should be fine to pass gamma is as a dummy even if not using fused rmsnorm
                ln_w,
                gate_w,  # gate_w
                gate_w_scale,
                up_w,  # up_w
                up_w_scale,
                down_w,  # down_w
                down_w_scale,
                lower_bound,
                output_tensor,  # out
                # Run RMSNorm inside the kernel if NOT using SP rmsnorm
                fused_rmsnorm=fused_rmsnorm,
                eps=self.rms_norm_eps,
                kernel_name="MLP",
            )
            residual = None

        # All-reduce or reduce-scatter, depending on whether SP is enabled
        if self.sequence_parallel_enabled:
            output_tensor = reduce_scatter_to_sequence_parallel_region(
                output_tensor, self.sequence_dimension, process_group=get_tp_group(self.config)
            )
        else:
            output_tensor = reduce_from_tensor_model_parallel_region(output_tensor)

        logger.debug(f"Quantized MLP output shape {output_tensor.shape}")
        return (output_tensor, residual)

    def _kernel_enabled_mlp(self, x, fused_rmsnorm, rmsnorm, residual, adapter_ids):
        fused_residual = residual is not None
        logger.debug(
            f"MLP: kernel, fused_residual={fused_residual}, fused_rmsnorm={fused_rmsnorm}, logical_neuron_cores={self.logical_neuron_cores}"
        )

        # Choose which kernel to call
        if fused_residual:
            assert (
                not self.sequence_parallel_enabled
            ), "MLP kernel cannot have both fused residual add and sequence parallel RMSnorm!"
            # Using fused residual add
            _mlp_fwd_call = nki_jit()(mlp_fused_add_isa_kernel)
        else:
            _mlp_fwd_call = nki_jit()(mlp_isa_kernel)

        if self.sequence_parallel_enabled:
            x = gather_from_sequence_parallel_region(
                x, self.sequence_dimension, process_group=get_tp_group(self.config)
            )

        # Build output tensor
        output_tensor_seqlen = x.shape[1]
        if fused_residual:
            # seqlen dim is doubled to store the residual add output
            output_tensor_seqlen *= 2

        output_tensor = torch.zeros(
            size=(
                x.shape[0],  # batch size
                output_tensor_seqlen,
                self.hidden_size,  # hidden size
            ),
            dtype=x.dtype,
            device=x.device,
        )

        # Grab weights
        # all weights of the layers are stored in (out, in) shape
        # unsqueeze so that shape of RMS gamma weight is [1, hidden] instead of [hidden]
        ln_w = rmsnorm.weight.unsqueeze(0)
        gate_w = self.gate_proj.weight.data
        up_w = self.up_proj.weight.data
        down_w = self.down_proj.weight.data

        grid = (vnc(self.logical_neuron_cores),)

        if fused_residual:
            _mlp_fwd_call[grid](
                x,  # attn_output
                residual,  # hidden
                ln_w,  # ln_w
                gate_w,  # gate_w
                up_w,  # up_w
                down_w,  # down_w
                output_tensor,  # out
                fused_rmsnorm=fused_rmsnorm,
                eps=self.rms_norm_eps,
                kernel_name="MLP",
                store_add=True,
            )
            original_seqlen = x.shape[1]
            residual = output_tensor[:, original_seqlen:, :]
            output_tensor = output_tensor[:, :original_seqlen, :]
        else:
            _mlp_fwd_call[grid](
                x,  # hidden
                # should be fine to pass gamma is as a dummy even if not using fused rmsnorm
                ln_w,
                gate_w,
                up_w,
                down_w,
                output_tensor,  # out
                # Run RMSNorm inside the kernel if NOT using SP rmsnorm
                fused_rmsnorm=fused_rmsnorm,
                eps=self.rms_norm_eps,
                kernel_name="MLP",
            )
            residual = None

        # All-reduce or reduce-scatter, depending on whether SP is enabled
        if self.sequence_parallel_enabled:
            output_tensor = reduce_scatter_to_sequence_parallel_region(
                output_tensor, self.sequence_dimension, process_group=get_tp_group(self.config)
            )
        else:
            output_tensor = reduce_from_tensor_model_parallel_region(
                output_tensor, process_group=get_tp_group(self.config)
            )

        logger.debug(f"MLP output shape {output_tensor.shape}")
        return (output_tensor, residual)

    def _native_mlp(self, x, rmsnorm, adapter_ids=None):
        logger.debug("MLP: native compiler")
        # all-gather is done here instead of CPL layers to
        # avoid 2 all-gathers from up and gate projections
        if self.sequence_parallel_enabled:
            x = gather_from_sequence_parallel_region(
                x, self.sequence_dimension, process_group=get_tp_group(self.config)
            )

        gate_proj_output = (
            self.gate_proj(x)
            if not is_lora_module(self.gate_proj)
            else self.gate_proj(x, adapter_ids)
        )
        up_proj_output = (
            self.up_proj(x) if not is_lora_module(self.up_proj) else self.up_proj(x, adapter_ids)
        )
        down_proj_input = self.act_fn(gate_proj_output) * up_proj_output
        output = (
            self.down_proj(down_proj_input)
            if not is_lora_module(self.up_proj)
            else self.down_proj(down_proj_input, adapter_ids)
        )
        logger.debug(f"MLP output shape {output.shape}")
        return output

    def forward(self, x, rmsnorm=None, residual=None, adapter_ids=None):
        """
        If residual is passed in, will fuse its add into the MLP kernel

        Returns a tuple of (output, residual), where residual is the output of the residual add
        """
        if self.mlp_kernel_enabled:
            fused_rmsnorm = not self.sequence_parallel_enabled
            # Quantized MLP kernel
            if self.quantized_mlp_kernel_enabled:
                return self._kernel_enabled_quantized_mlp(
                    x, fused_rmsnorm, rmsnorm, residual, adapter_ids=adapter_ids
                )
            # MLP kernel
            return self._kernel_enabled_mlp(
                x, fused_rmsnorm, rmsnorm, residual, adapter_ids=adapter_ids
            )
        else:
            # No kernel
            return (self._native_mlp(x, rmsnorm, adapter_ids=adapter_ids), None)


@register_module("NeuronLlamaAttention")
class NeuronLlamaAttention(NeuronAttentionBase):
    """
    Compared with LlamaAttention, this class just
    1. replaces the q_proj, k_proj, v_proj with column parallel layer
    2. replaces the o_proj with row parallel layer
    3. update self.num_head to be self.num_head / tp_degree
    4. update self.num_key_value_heads to be self.num_key_value_heads / tp_degree
    5. update forward() method to adjust to changes from self.num_head
    """

    def __init__(self, config: InferenceConfig, tensor_model_parallel_group=None):
        super().__init__(tensor_model_parallel_group=tensor_model_parallel_group)

        self.config = config
        self.neuron_config = config.neuron_config
        self.hidden_size = config.hidden_size
        self.num_attention_heads = config.num_attention_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.head_dim = self.hidden_size // self.num_attention_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.padding_side = config.neuron_config.padding_side
        self.torch_dtype = config.neuron_config.torch_dtype
        self.is_medusa = config.neuron_config.is_medusa
        self.flash_decoding_enabled = config.neuron_config.flash_decoding_enabled
        self.num_cores_per_group = config.num_cores_per_group
        self.bias = getattr(config, "attention_bias", False)
        self.rpl_reduce_dtype = config.neuron_config.rpl_reduce_dtype
        self.mlp_kernel_enabled = config.neuron_config.mlp_kernel_enabled
        self.rms_norm_eps = config.rms_norm_eps

        if parallel_state.model_parallel_is_initialized():
            self.tp_degree = self.config.neuron_config.tp_degree
        else:
            self.tp_degree = 1

        self.fused_qkv = config.neuron_config.fused_qkv
        self.clip_qkv = None

        self.sequence_parallel_enabled = self.neuron_config.sequence_parallel_enabled
        self.sequence_dimension = 1 if self.sequence_parallel_enabled else None
        logger.debug(
            f"Hello from NeuronLlamaAttention init! Is SP enabled? {self.sequence_parallel_enabled}. Dim? {self.sequence_dimension}"
        )

        self.init_gqa_properties()

        self.init_rope()

    def init_rope(self):
        if not hasattr(self.config, "rope_scaling") or self.config.rope_scaling is None:
            # TODO(yihsian): Check if we can just use our own implementation
            if self.is_medusa:
                self.rotary_emb = LlamaRotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    base=self.rope_theta,
                )
            else:
                self.rotary_emb = RotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    base=self.rope_theta,
                )
        else:
            rope_type = self.config.rope_scaling.get(
                "rope_type", self.config.rope_scaling.get("type", None)
            )
            if rope_type == "llama3":
                self.rotary_emb = Llama3RotaryEmbedding(
                    dim=self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    base=self.rope_theta,
                    factor=self.config.rope_scaling["factor"],
                    low_freq_factor=self.config.rope_scaling["low_freq_factor"],
                    high_freq_factor=self.config.rope_scaling["high_freq_factor"],
                    original_max_position_embeddings=self.config.rope_scaling[
                        "original_max_position_embeddings"
                    ],
                )
            else:
                # LlamaRotaryEmbedding automatically chooses the correct scaling type from config.
                # Warning: The HF implementation may have precision issues when run on Neuron.
                # We include it here for compatibility with other scaling types.
                self.rotary_emb = LlamaRotaryEmbedding(self.config)


    def scaled_qk(self, Q, K, attention_mask): # computing the scaled dot-product attention scores using NKI (block_matmul_nki --> uses NKI matmul)
        ## [32,64] [64,32]
        bs, head, sequence, dimension = Q.size()
        _, head_k, sequence_k, dimension_k = K.size()
        # print("size:", sequence)
        # print("size2:", sequence_k)
        result = torch.zeros((bs, head, sequence, sequence_k), device = Q.device, dtype=Q.dtype)
        # print("Q.type:", Q.dtype)
        for i in range(bs):
            for j in range(head):
                temp_q = Q[i, j, :, :]
                temp_k = (K.transpose(2, 3))[i, j, :, :]
                # print("temp_q", temp_q.size())
                # print("temp_k", temp_k.size())
                temp = block_matmul_nki(temp_q, temp_k)
                result[i, j, :, :] = temp
        QK = result / math.sqrt(self.head_dim)

        # QK = torch.matmul(Q, K.transpose(2, 3)) / math.sqrt(self.head_dim)
        QK = torch.where(attention_mask, QK, torch.finfo(QK.dtype).min)
        return QK



# TODO: Modularize RotaryEmbedding. See how HF transformers does it in 4.43.
class Llama3RotaryEmbedding(nn.Module):
    """
    Adapted from Llama 4.43 impl
    * https://github.com/huggingface/transformers/blob/v4.43.4/src/transformers/models/llama/modeling_llama.py#L78
    * https://github.com/huggingface/transformers/blob/v4.43.4/src/transformers/modeling_rope_utils.py#L345

    This implementation ensures inv_freq is calculated and stored in fp32.
    """

    def __init__(
        self,
        dim,
        max_position_embeddings=131072,
        base=500000.0,
        factor=8.0,
        low_freq_factor=1.0,
        high_freq_factor=4.0,
        original_max_position_embeddings=8192,
    ):
        super().__init__()
        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        self.factor = factor
        self.low_freq_factor = low_freq_factor
        self.high_freq_factor = high_freq_factor
        self.old_context_len = original_max_position_embeddings
        self.register_buffer("inv_freq", None, persistent=False)

    @torch.no_grad()
    def forward(self, x, position_ids):
        # x: [bs, num_attention_heads, seq_len, head_size]
        if self.inv_freq is None:
            inv_freq = 1.0 / (
                self.base
                ** (torch.arange(0, self.dim, 2, dtype=torch.int64).float().to(x.device) / self.dim)
            )

            low_freq_wavelen = self.old_context_len / self.low_freq_factor
            high_freq_wavelen = self.old_context_len / self.high_freq_factor
            new_freqs = []
            for freq in inv_freq:
                wavelen = 2 * math.pi / freq
                if wavelen < high_freq_wavelen:
                    new_freqs.append(freq)
                elif wavelen > low_freq_wavelen:
                    new_freqs.append(freq / self.factor)
                else:
                    assert low_freq_wavelen != high_freq_wavelen
                    smooth = (self.old_context_len / wavelen - self.low_freq_factor) / (
                        self.high_freq_factor - self.low_freq_factor
                    )
                    new_freqs.append((1 - smooth) * freq / self.factor + smooth * freq)
            self.inv_freq = torch.tensor(new_freqs, dtype=inv_freq.dtype, device=inv_freq.device)

        inv_freq_expanded = (
            self.inv_freq[None, :, None].float().expand(position_ids.shape[0], -1, 1)
        )
        position_ids_expanded = position_ids[:, None, :].float()
        with torch.autocast(device_type=x.device.type, enabled=False):
            freqs = (inv_freq_expanded.float() @ position_ids_expanded.float()).transpose(1, 2)
            emb = torch.cat((freqs, freqs), dim=-1)
            cos = emb.cos()
            sin = emb.sin()
        return cos.to(dtype=x.dtype), sin.to(dtype=x.dtype)


class NeuronLlamaDecoderLayer(nn.Module):
    """
    Just replace the attention with the NXD version, and MLP with the NXD version
    """

    def __init__(self, config: InferenceConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = _LLAMA_MODULE_MAP[config.neuron_config.attn_cls](
            config=config, tensor_model_parallel_group=get_tp_group(config)
        )
        self.mlp = NeuronLlamaMLP(config)
        logger.debug(
            f"Instantiating RMSNorm modules with hidden size {config.hidden_size} and EPS {config.rms_norm_eps}"
        )
        self.input_layernorm = None
        if (
            not config.neuron_config.is_eagle_draft
            or config.neuron_config.enable_eagle_draft_input_norm
        ):
            self.input_layernorm = get_rmsnorm_cls()(
                config.hidden_size,
                eps=config.rms_norm_eps,
                nki_enabled=config.neuron_config.nki_enabled,
            )
        self.post_attention_layernorm = get_rmsnorm_cls()(
            config.hidden_size,
            eps=config.rms_norm_eps,
            nki_enabled=config.neuron_config.nki_enabled,
        )
        self.qkv_kernel_enabled = config.neuron_config.qkv_kernel_enabled
        self.mlp_kernel_enabled = config.neuron_config.mlp_kernel_enabled
        self.rmsnorm_quantize_kernel_enabled = config.neuron_config.rmsnorm_quantize_kernel_enabled
        self.mlp_kernel_fuse_residual_add = config.neuron_config.mlp_kernel_fuse_residual_add
        self.sequence_parallel_enabled = config.neuron_config.sequence_parallel_enabled
        self.config = config

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        adapter_ids=None,
        **kwargs,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        residual = hidden_states

        # RMSNorm (fused with QKV kernel when SP is disabled)
        if (not self.qkv_kernel_enabled or self.sequence_parallel_enabled) and self.input_layernorm:
            hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, present_key_value, cos_cache, sin_cache = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            adapter_ids=adapter_ids,
            rmsnorm=self.input_layernorm,
            **kwargs,
        )

        if self.mlp_kernel_enabled and self.mlp_kernel_fuse_residual_add:
            assert (
                not self.sequence_parallel_enabled
            ), "mlp_kernel_fuse_residual_add should be off when sequence parallelism is enabled"
            # First residual add handled in the MLP kernel
            hidden_states, residual = self.mlp(
                hidden_states,
                rmsnorm=self.post_attention_layernorm,
                residual=residual,
                adapter_ids=adapter_ids,
            )
        else:
            hidden_states = residual + hidden_states
            residual = hidden_states
            # RMSNorm (fused with QKV kernel when SP is disabled)
            if not self.mlp_kernel_enabled or self.sequence_parallel_enabled:
                hidden_states = self.post_attention_layernorm(hidden_states)
            hidden_states, _ = self.mlp(
                hidden_states,
                rmsnorm=self.post_attention_layernorm,
                adapter_ids=adapter_ids,
            )

        hidden_states = residual + hidden_states

        outputs = (hidden_states, present_key_value, cos_cache, sin_cache)
        return outputs


class ResBlock(nn.Module):
    """
    A Residual Block module.

    This module performs a linear transformation followed by a SiLU activation,
    and then adds the result to the original input, creating a residual connection.

    Args:
        hidden_size (int): The size of the hidden layers in the block.
    """

    def __init__(self, hidden_size):
        super().__init__()
        self.linear = nn.Linear(hidden_size, hidden_size)
        # Initialize as an identity mapping
        torch.nn.init.zeros_(self.linear.weight)
        # Use SiLU activation to keep consistent with the Llama model
        self.act = nn.SiLU()

    def forward(self, x):
        """
        Forward pass of the ResBlock.

        Args:
            x (torch.Tensor): Input tensor.

        Returns:
            torch.Tensor: Output after the residual connection and activation.
        """
        return x + self.act(self.linear(x))
    



class NeuronLlamaModel(NeuronBaseModel):
    """
    The neuron version of the LlamaModel
    """

    def setup_attr_for_model(self, config: InferenceConfig):
        # Needed for init_inference_optimization()
        self.on_device_sampling = config.neuron_config.on_device_sampling_config is not None
        self.tp_degree = config.neuron_config.tp_degree
        self.hidden_size = config.hidden_size
        self.num_attention_heads = config.num_attention_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.max_batch_size = config.neuron_config.max_batch_size
        self.buckets = config.neuron_config.buckets

    def init_model(self, config: InferenceConfig):
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        if parallel_state.model_parallel_is_initialized():
            self.embed_tokens = ParallelEmbedding(
                config.vocab_size,
                config.hidden_size,
                self.padding_idx,
                dtype=config.neuron_config.torch_dtype,
                shard_across_embedding=not config.neuron_config.vocab_parallel,
                sequence_parallel_enabled=False,
                pad=True,
                tensor_model_parallel_group=get_tp_group(config),
                use_spmd_rank=config.neuron_config.vocab_parallel,
            )

            self.lm_head = ColumnParallelLinear(
                config.hidden_size,
                config.vocab_size,
                gather_output=not self.on_device_sampling,
                bias=False,
                pad=True,
                tensor_model_parallel_group=get_tp_group(config),
            )
        else:
            self.embed_tokens = nn.Embedding(
                config.vocab_size,
                config.hidden_size,
                self.padding_idx,
            )
            self.lm_head = nn.Linear(
                config.hidden_size,
                config.vocab_size,
                bias=False,
            )

        # In the target fp8 checkpoint, the 1st and last
        # layers are not using fp8.
        updated_configs = []
        for i in range(config.num_hidden_layers):
            # TODO: Remove hardcoded code to have non-quantized MLPs for first and last decoder block
            if i == 0 or i == config.num_hidden_layers - 1:
                non_quant_config = copy.deepcopy(config)
                non_quant_config.neuron_config.quantized_mlp_kernel_enabled = False
                updated_configs.append(non_quant_config)
            else:
                updated_configs.append(config)
        self.layers = nn.ModuleList([NeuronLlamaDecoderLayer(conf) for conf in updated_configs])
        if not config.neuron_config.is_eagle_draft:
            self.norm = get_rmsnorm_cls()(config.hidden_size, eps=config.rms_norm_eps, nki_enabled=config.neuron_config.nki_enabled)

        if config.neuron_config.is_eagle_draft:
            fc_bias = getattr(config, "fc_bias", False)
            self.fc = ColumnParallelLinear(
                config.hidden_size * 2, config.hidden_size, bias=fc_bias, gather_output=True
            )
        self.is_medusa = config.neuron_config.is_medusa
        self.num_medusa_heads = config.neuron_config.num_medusa_heads
        self.medusa_speculation_length = config.neuron_config.medusa_speculation_length

        if self.is_medusa:
            if parallel_state.model_parallel_is_initialized():
                medusa_head_cls = ColumnParallelLinear
            else:
                medusa_head_cls = nn.Linear
            for i in range(self.num_medusa_heads):
                medusa_head = nn.Sequential(
                    *([ResBlock(config.hidden_size)] * 1),
                    medusa_head_cls(
                        config.hidden_size,
                        config.vocab_size,
                        gather_output=not self.on_device_sampling,
                        bias=False,
                    ),
                )
                setattr(self, f"medusa_head_{i}", medusa_head)


class NeuronLlamaForCausalLM(NeuronBaseForCausalLM):
    """
    This class extends LlamaForCausalLM create traceable
    blocks for Neuron.

    Args:
        LlamaForCausalLM (_type_): _description_
    """

    _model_cls = NeuronLlamaModel

    @staticmethod
    def load_hf_model(model_path):
        return LlamaForCausalLM.from_pretrained(model_path)

    @staticmethod
    def convert_hf_to_neuron_state_dict(state_dict: dict, config: InferenceConfig) -> dict:
        """This function should be over-ridden in child classes as needed"""
        neuron_config = config.neuron_config
        if neuron_config.fused_qkv:
            state_dict = convert_state_dict_to_fused_qkv(state_dict, config)

        if neuron_config.vocab_parallel:
            # TODO: this hack can be removed after replication_id is ready to use
            state_dict["embed_tokens.rank_util.rank"] = torch.arange(
                0, neuron_config.local_ranks_size
            )

        # to facilitate rank usage in attention
        num_layers = config.num_hidden_layers
        tp_degree = neuron_config.tp_degree
        for i in range(num_layers):
            state_dict[f"layers.{i}.self_attn.rank_util.rank"] = torch.arange(
                0, tp_degree, dtype=torch.int32
            )
        # to facilitate rank usage in base model
        state_dict["rank_util.rank"] = torch.arange(0, tp_degree, dtype=torch.int32)
        return state_dict

    @staticmethod
    def update_state_dict_for_tied_weights(state_dict):
        state_dict["lm_head.weight"] = state_dict["embed_tokens.weight"].clone()

    @classmethod
    def get_config_cls(cls):
        return LlamaInferenceConfig
