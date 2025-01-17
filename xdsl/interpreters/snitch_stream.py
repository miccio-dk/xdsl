from collections.abc import Sequence
from dataclasses import dataclass
from functools import reduce
from itertools import accumulate
from operator import mul
from typing import Any

from typing_extensions import Self

from xdsl.dialects import snitch_stream
from xdsl.interpreter import (
    Interpreter,
    InterpreterFunctions,
    PythonValues,
    impl,
    register_impls,
)
from xdsl.interpreters.riscv import RawPtr
from xdsl.interpreters.stream import (
    ReadableStream,
    WritableStream,
)
from xdsl.ir.affine import AffineExpr, AffineMap


def indexing_map_from_bounds(bounds: Sequence[int]) -> AffineMap:
    """
    Given a set of upper bounds of the nested loop, creates a map that represents the
    values of the loop iterators.

    e.g.:
    ```
    for i in range(2):
        for j in range(3):
            print(i, j) # -> (0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)

    map = indexing_map_from_bounds([3, 2])

    for k in range(6):
        print(map.eval(k)) # -> (0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)
    ```
    """
    divs = tuple(accumulate(bounds, mul, initial=1))[-2::-1]
    return AffineMap(
        1,
        0,
        tuple(
            (
                AffineExpr.dimension(0).floor_div(div) % bound
                if div != 1
                else AffineExpr.dimension(0) % bound
            )
            for bound, div in zip(reversed(bounds), divs, strict=True)
        ),
    )


def offset_map_from_strides(strides: Sequence[int]) -> AffineMap:
    """
    Given a set of offsets for each bound of the nested loop, creates a map that
    represents the offset from the base_ptr that the stream will fetch.

    e.g.:
    ```
    my_list = [1, 2, 3, 4, 5, 6]
    strides = [1, 3]
    for i in range(2):
        for j in range(3):
            k = i * 3 + j
            el = my_list[k]
            print(el) # -> 1, 2, 3, 4, 5, 6

    map = offset_map_from_strides([1, 3])

    for i in range(2):
        for j in range(3):
            k = map.eval(i, j)
            el = my_list[k]
            print(el) # -> 1, 2, 3, 4, 5, 6
    ```
    """
    if not strides:
        # Return empty map to avoid reducing over an empty sequence
        return AffineMap(1, 0, ())

    return AffineMap(
        len(strides),
        0,
        (
            reduce(
                lambda acc, m: acc + m,
                (
                    AffineExpr.dimension(i) * stride
                    for i, stride in enumerate(reversed(strides))
                ),
            ),
        ),
    )


@dataclass
class StridePattern:
    """
    Defines the upper bounds and strides for the stride pattern, conceptually from the
    innermost loop outwards.
    """

    ub: list[int]
    strides: list[int]

    @property
    def offset_expr(self) -> AffineExpr:
        """
        Creates the map that represents the offset that the stream will read from or write
        to at register access "i".
        """
        indexing_map = indexing_map_from_bounds(self.ub)
        offset_map = offset_map_from_strides(self.strides)
        result_map = offset_map.compose(indexing_map)
        return result_map.results[0]

    @classmethod
    def from_attr(cls, attr: snitch_stream.StridePattern) -> Self:
        return cls(
            [ub.data for ub in attr.ub],
            [stride.data for stride in attr.strides],
        )


@dataclass
class StridedPointerInputStream(ReadableStream[float]):
    offset_expr: AffineExpr
    pointer: RawPtr
    index = -1

    def read(self) -> float:
        self.index += 1
        offset = self.offset_expr.eval((self.index,), ())
        return (self.pointer + offset).float64[0]


@dataclass
class StridedPointerOutputStream(WritableStream[float]):
    index = -1
    offset_expr: AffineExpr
    pointer: RawPtr

    def write(self, value: float) -> None:
        self.index += 1
        offset = self.offset_expr.eval((self.index,), ())
        (self.pointer + offset).float64[0] = value


@register_impls
class SnitchStreamFunctions(InterpreterFunctions):
    @impl(snitch_stream.StreamingRegionOp)
    def run_streaming_region(
        self,
        interpreter: Interpreter,
        op: snitch_stream.StreamingRegionOp,
        args: tuple[Any, ...],
    ) -> PythonValues:
        input_stream_count = len(op.inputs)
        output_stream_count = len(op.outputs)
        input_pointers: tuple[RawPtr, ...] = args[:input_stream_count]
        output_pointers: tuple[RawPtr, ...] = args[
            input_stream_count : input_stream_count + output_stream_count
        ]

        stride_patterns = tuple(
            StridePattern.from_attr(pattern) for pattern in op.stride_patterns
        )
        if len(stride_patterns) == 1:
            pattern = stride_patterns[0]
            input_stride_patterns = (pattern,) * input_stream_count
            output_stride_patterns = (pattern,) * output_stream_count
        else:
            input_stride_patterns = stride_patterns[:input_stream_count]
            output_stride_patterns = stride_patterns[input_stream_count:]

        input_streams = tuple(
            StridedPointerInputStream(pat.offset_expr, ptr)
            for pat, ptr in zip(input_stride_patterns, input_pointers, strict=True)
        )

        output_streams = tuple(
            StridedPointerOutputStream(pat.offset_expr, ptr)
            for pat, ptr in zip(output_stride_patterns, output_pointers, strict=True)
        )

        interpreter.run_ssacfg_region(
            op.body, (*input_streams, *output_streams), "steraming_region"
        )

        return ()
