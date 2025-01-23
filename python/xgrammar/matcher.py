"""Match the output of the LLM to the specified grammar, then generate the mask for the next
token."""

import math
from typing import List, Optional, Tuple, Union

import torch

from .base import XGRObject, _core
from .compiler import CompiledGrammar
from .kernels import apply_token_bitmask_inplace_cpu, apply_token_bitmask_inplace_triton

"""The dtype of the bitmask: int32."""
bitmask_dtype = torch.int32


_is_cuda_available = torch.cuda.is_available()


def get_bitmask_shape(batch_size: int, vocab_size: int) -> Tuple[int, int]:
    """Return the shape of the bitmask: (batch_size, ceil(vocab_size / 32))"""
    return (batch_size, math.ceil(vocab_size / 32))


_FULL_MASK = torch.tensor(-1, dtype=bitmask_dtype)


def allocate_token_bitmask(batch_size: int, vocab_size: int) -> torch.Tensor:
    """Allocate the bitmask for the next token prediction. The bitmask is an int32 tensor on
    CPU with shape (batch_size, ceil(vocab_size / 32)). Users who have their own needs to
    manage CUDA memory can construct the tensor with get_bitmask_shape and bitmask_dtype
    themselves.

    The reason why we use int32 instead of uint32 is that old versions of PyTorch do not support
    uint32.

    Parameters
    ----------
    batch_size : int
        The batch size of the bitmask.

    vocab_size : int
        The size of the vocabulary.

    Returns
    -------
    bitmask : torch.Tensor
        The shape of the bitmask.
    """
    # In CUDA, use pinned memory to speed up data transfer from CPU to GPU
    return torch.full(
        get_bitmask_shape(batch_size, vocab_size),
        _FULL_MASK,
        dtype=bitmask_dtype,
        pin_memory=_is_cuda_available,
    )


def reset_token_bitmask(bitmask: torch.Tensor) -> None:
    """Reset the bitmask to the full mask."""
    bitmask.fill_(_FULL_MASK)


def apply_token_bitmask_inplace(
    logits: torch.Tensor,
    bitmask: torch.Tensor,
    *,
    indices: Optional[List[int]] = None,
) -> None:
    """Apply the bitmask to the logits in-place. The bitmask is a 01 bitwise compressed tensor,
    where 0 means the token is masked and 1 means the token is not masked. It can be generated by
    allocate_token_bitmask and filled by fill_next_token_bitmask. After applying the bitmask, the
    masked logits will be set to -inf.

    The shape of logits and bitmask should be (batch_size, vocab_size) and
    (batch_size, bitmask_size) respectively. bitmask_size = ceil(vocab_size / 32). The operation is:

    .. code:: python

        for i in range(batch_size):
            for j in range(vocab_size):
                if get_bitmask_value(bitmask, i, j) == 0:
                    logits[i, j] = -inf

    get_bitmask_value(bitmask, i, j) gets the j-th bit of the i-th row of the bitmask.

    Indices can be used to specify which logits in the batch to apply the bitmask to. It is
    especially useful when there are structured requests and unstructured requests mixed in the
    same batch by skipping masking the logits in the unstructured requests. When specified, the
    operation will be

    .. code:: python

        for batch_id in indices:
            for j in range(vocab_size):
                if get_bitmask_value(bitmask, batch_id, j) == 0:
                    logits[batch_id, j] = -inf

    The logits and bitmask should be on the same device. If both them are on CUDA, we launch a CUDA
    kernel to apply bitmask. If both them are on CPU, we use a CPU implementation. The CUDA kernel
    is optimized and should be preferred.

    In practice, the bitmask is allocated on CPU, and the logits is usually on GPU, so users should
    manually copy the bitmask to GPU before calling this function.

    Parameters
    ----------
    logits : torch.Tensor
        The tensor to apply the bitmask to.

    bitmask : torch.Tensor
        The bitmask to apply.

    indices : Optional[List[int]], default: None
        A list of indices to specify which logits in the batch to apply the bitmask to. If None,
        apply the bitmask to all logits in the batch.
    """
    if bitmask.device != logits.device:
        raise ValueError(
            "logits and bitmask should be on the same device. "
            + f"But got logits.device: {logits.device}, bitmask.device: {bitmask.device}"
        )

    if logits.device.type == "cuda":
        apply_token_bitmask_inplace_triton(logits, bitmask, indices)
    elif logits.device.type == "cpu":
        apply_token_bitmask_inplace_cpu(logits, bitmask, indices)
    else:
        raise ValueError("Currently, logit masking is only supported on CUDA or CPU.")


class GrammarMatcher(XGRObject):
    """Match the output of the LLM to the specified grammar, then generate the mask for the next
    token. This is the core class in the grammar-guided generation.

    This class maintains a stateful matcher that can accept tokens and strings, then match them
    to the specified grammar. The matcher can provide a bitmask for the next token prediction,
    so that the output of the LLM follows the specified grammar. Its state can be reset and
    rolled back by tokens. It also provides utilities for jump-forward decoding.

    After matching the whole grammar, the matcher will accept a stop token. The token mask at
    this time will only allow stop tokens. After accepting the stop token, the matcher will
    terminate, then it cannot accept any new token or generate a new token mask, meaning the
    generation is finished.

    Under the hood, it utilizes a pushdown automaton with backtracking to match the grammar,
    with optimizations specific to LLM token mask generation.

    Parameters
    ----------
    compiled_grammar : CompiledGrammar
        The initialization context for the grammar matcher.

    override_stop_tokens : Optional[Union[int, List[int]]], default: None
        If not None, the stop tokens to override the ones in the grammar.

    terminate_without_stop_token : bool, default: False
        Whether to terminate the matcher without accepting a stop token.

    max_rollback_tokens : int, default: 0
        The maximum number of rollback tokens allowed. The rollback operation is useful for
        jump-forward decoding and speculative decoding.
    """

    def __init__(
        self,
        compiled_grammar: CompiledGrammar,
        *,
        override_stop_tokens: Optional[Union[int, List[int]]] = None,
        terminate_without_stop_token: bool = False,
        max_rollback_tokens: int = 0,
    ) -> None:
        if not isinstance(compiled_grammar, CompiledGrammar):
            raise ValueError("The grammar should be compiled before passing it to GrammarMatcher.")

        if isinstance(override_stop_tokens, int):
            override_stop_tokens = [override_stop_tokens]

        self._init_handle(
            _core.GrammarMatcher(
                compiled_grammar._handle,
                override_stop_tokens,
                terminate_without_stop_token,
                max_rollback_tokens,
            )
        )

    def accept_token(self, token_id: int, *, debug_print: bool = False) -> bool:
        """Accept one token and update the state of the matcher.

        Parameters
        ----------
        token_id : int
            The id of the token to accept.

        debug_print : bool, default: False
            Whether to print information about the internal state of the matcher. Helpful
            for debugging.

        Returns
        -------
        accepted : bool
            Whether the token is accepted.
        """
        return self._handle.accept_token(token_id, debug_print)

    def fill_next_token_bitmask(
        self, bitmask: torch.Tensor, index: int = 0, *, debug_print: bool = False
    ) -> None:
        """Fill the bitmask for the next token prediction. The input bitmask can be generated
        by allocate_token_bitmask, and must be on CPU. bitmask[index] will be filled with the
        next token bitmask.

        This method does not change the matcher state.

        Parameters
        ----------
        bitmask : torch.Tensor
            The bitmask for the next token prediction.

        index : int, default: 0
            The batch id of the bitmask.
        """
        if bitmask.device.type != "cpu":
            raise ValueError("bitmask should be on CPU.")
        if bitmask.dtype != bitmask_dtype:
            raise ValueError(f"bitmask should be of type {bitmask_dtype}.")
        self._handle.fill_next_token_bitmask(
            bitmask.data_ptr(), list(bitmask.shape), index, debug_print
        )

    def find_jump_forward_string(self) -> str:
        """Find the jump-forward string for jump-forward decoding. This is the longest string that
        certainly conforms with the current grammar from the current matcher state. This string
        can become the output of the LLM without requiring LLM decoding.

        This method does not change the matcher state.

        Returns
        -------
        jump_forward_string : str
            The jump-forward string.
        """
        return self._handle.find_jump_forward_string()

    def rollback(self, num_tokens: int = 1) -> None:
        """Rollback the matcher to a previous state by several tokens.

        Parameters
        ----------
        num_tokens : int, default: 1
            The number of tokens to rollback. It cannot exceed the current number of steps, nor can
            it exceed the specified maximum number of rollback tokens.
        """
        self._handle.rollback(num_tokens)

    def is_terminated(self) -> bool:
        """Check if the matcher has terminated. If terminate_without_stop_token is False, the
        matcher will terminate if it has accepted the stop token. Otherwise, the matcher will
        terminate after matching the whole grammar.

        Returns
        -------
        terminated : bool
            Whether the matcher has terminated.
        """
        return self._handle.is_terminated()

    def reset(self) -> None:
        """Reset the matcher to the initial state."""
        return self._handle.reset()

    @property
    def max_rollback_tokens(self) -> int:
        """Get the maximum number of rollback tokens allowed.

        Returns
        -------
        max_rollback_tokens : int
            The maximum number of rollback tokens.
        """
        return self._handle.max_rollback_tokens

    @property
    def stop_token_ids(self) -> List[int]:
        """The ids of the stop tokens used in the matcher. If specified, the provided stop tokens
        will be used. Otherwise, the stop tokens will be detected from the vocabulary.

        Returns
        -------
        stop_token_ids : List[int]
            The ids of the stop tokens.
        """
        return self._handle.stop_token_ids

    def _debug_accept_string(
        self, input_str: Union[str, bytes], *, debug_print: bool = False
    ) -> bool:
        """Accept a string and update the state of the matcher. The whole string is considered
        as one step in rollback. It is only used to complement the functionality of accept_token.

        Parameters
        ----------
        input_str : Union[str, bytes]
            The string to be accepted.

        debug_print : bool, default: False
            Whether to print information about the internal state of the matcher. Helpful for
            debugging.

        Returns
        -------
        accepted : bool
            Whether the string is accepted.
        """
        return self._handle._debug_accept_string(input_str, debug_print)
