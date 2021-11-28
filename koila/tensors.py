from __future__ import annotations

import builtins
import dataclasses as dcls
import functools
import logging
from dataclasses import dataclass
from functools import wraps
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    NamedTuple,
    NoReturn,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    final,
    overload,
)

import torch
from rich.logging import RichHandler
from torch import Tensor
from torch import device as Device
from torch import dtype as DType

from . import interfaces, shapes
from .errors import NeverImplementedError, UnsupportedError
from .interfaces import Runnable, RunnableTensor, TensorLike
from .partials import PartialInfo
from .shapes import MetaData, Shape, ShapeFunction

T = TypeVar("T")
V = TypeVar("V", contravariant=True)

logger = logging.getLogger(__name__)
logger.addHandler(RichHandler())
logger.setLevel(logging.WARNING)


@dataclass(frozen=True)
class LazyFunction(Generic[V]):
    func: Callable[..., Tensor]
    shape_func: ShapeFunction

    def __call__(self, *args: Any, **kwargs: Any) -> LazyTensor:
        lazy_args = tuple(lazy(arg) for arg in args)
        lazy_kwargs = dict((k, lazy(v)) for (k, v) in kwargs.items())
        shape = self.shape_func(*args, **kwargs)
        return LazyTensor(Evaluation(self.func, shape, None, *lazy_args, **lazy_kwargs))

    def __get__(self, obj: V, objtype: Type[V]) -> Callable[..., LazyTensor]:
        assert isinstance(obj, objtype), [type(obj), objtype]
        if obj is None:
            return self
        else:
            return functools.partial(self, obj)


@final
@dataclass(init=False)
class Evaluation(RunnableTensor):
    func: Callable[..., Tensor]
    shape: Shape
    callback: Callable[[Tensor], Tensor] | None = None
    args: Tuple[LazyTensor | Tensor | int | float | bool, ...] = dcls.field(
        default_factory=tuple
    )
    kwargs: Dict[str, LazyTensor | Tensor | int | float | bool] = dcls.field(
        default_factory=dict
    )

    def __init__(
        self,
        func: Callable[..., Tensor],
        shape: Shape,
        callback: Callable[[Tensor], Tensor] | None,
        *args: LazyTensor | Tensor | int | float | bool,
        **kwargs: LazyTensor | Tensor | int | float | bool,
    ) -> None:
        self.func = func
        self.shape = shape
        self.callback = callback
        self.args = args
        self.kwargs = kwargs

    def __hash__(self) -> int:
        # Evaluations are unique.
        return id(self)

    def run(self, partial: PartialInfo | None = None) -> Tensor:
        if partial is None:
            real_args = [run(arg) for arg in self.args]
            real_kwargs = {k: run(v) for (k, v) in self.kwargs.items()}
        else:
            real_args = [run(arg, partial) for arg in self.args]
            real_kwargs = {k: run(v, partial) for (k, v) in self.kwargs.items()}

        result = self.func(*real_args, **real_kwargs)

        if partial is not None:
            if self.callback is None:
                raise UnsupportedError("Cannot safely parallelize.")
            result = self.callback(result)

        assert self.shape == result.shape, [self.shape, result.shape]

        return result

    def size(self, dim: int | None = None) -> int | Tuple[int, ...]:
        shape = self.shape.value
        if dim is not None:
            return shape[dim]
        else:
            return shape

    def dtype(self) -> DType:
        return self.shape.metadata.dtype

    def device(self) -> str | Device:
        return self.shape.metadata.device

    def metadata(self) -> MetaData:
        return self.shape.metadata

    def batch(self) -> int | None:
        return self.shape.metadata.batch


@final
@dataclass(init=False, repr=False)
class LazyTensor(RunnableTensor):
    _data: TensorLike
    _batch: int | None = None

    def __init__(self, data: TensorLike, batch: int | None = None) -> None:
        if isinstance(data, LazyTensor):
            self._data = data._data
            self._batch = data._batch
        else:
            self._data = data
            self._batch = batch

    # Implementations

    def run(self, partial: PartialInfo | None = None) -> Tensor:
        data = self._data
        if isinstance(data, Tensor):
            if partial is None:
                return data
            else:
                return data[partial.index]

        return data.run(partial)

    @overload
    def size(self) -> Tuple[int, ...]:
        ...

    @overload
    def size(self, dim: int) -> int:
        ...

    def size(self, dim: int | None = None) -> int | Tuple[int, ...]:
        data = self._data

        if dim is None:
            return data.size()

        return data.size(dim)

    def dtype(self) -> DType:
        return interfaces.dtype(self._data)

    def device(self) -> str | Device:
        return interfaces.device(self._data)

    def metadata(self) -> MetaData:
        return interfaces.metadata(self._data)

    def batch(self) -> int | None:
        self._batch

    # Magic methods

    def __str__(self) -> str:
        return f"LazyTensor {self.run()}"

    def __bool__(self) -> bool:
        return bool(self.item())

    def __int__(self) -> int:
        return int(self.item())

    def __float__(self) -> float:
        return float(self.item())

    def __invert__(self) -> bool:
        return not bool(self)

    def __pos__(self) -> TensorLike:
        return lazy_forward(Tensor.__pos__, shapes.identity, self)

    def __neg__(self) -> TensorLike:
        return lazy_forward(Tensor.__neg__, shapes.identity, self)

    def __add__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__add__, shapes.symmetric, self, other)

    def __radd__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__add__, shapes.symmetric, other, self)

    def __sub__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__sub__, shapes.symmetric, self, other)

    def __rsub__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__sub__, shapes.symmetric, other, self)

    def __mul__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__mul__, shapes.symmetric, self, other)

    def __rmul__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__mul__, shapes.symmetric, other, self)

    def __truediv__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__truediv__, shapes.symmetric, self, other)

    def __rtruediv__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__truediv__, shapes.symmetric, other, self)

    def __floordiv__(self, other: TensorLike) -> NoReturn:
        del other
        raise NeverImplementedError

    def __rfloordiv__(self, other: TensorLike) -> NoReturn:
        del other
        raise NeverImplementedError

    def __pow__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__pow__, shapes.symmetric, self, other)

    def __rpow__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__pow__, shapes.symmetric, other, self)

    def __mod__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__mod__, shapes.symmetric, self, other)

    def __rmod__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__mod__, shapes.symmetric, other, self)

    def __divmod__(self, other: TensorLike) -> NoReturn:
        del other
        raise NeverImplementedError

    def __rdivmod__(self, other: TensorLike) -> NoReturn:
        del other
        raise NeverImplementedError

    def __abs__(self) -> TensorLike:
        return lazy_forward(Tensor.__abs__, shapes.identity, self)

    def __hash__(self) -> int:
        # LazyTensors are not unique. They are defined by their data.
        return id(self._data)

    def __matmul__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__matmul__, shapes.matmul, self, other)

    def __rmatmul__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__matmul__, shapes.matmul, other, self)

    def __eq__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__eq__, shapes.symmetric, self, other)

    def __ne__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__ne__, shapes.symmetric, self, other)

    def __gt__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__gt__, shapes.symmetric, self, other)

    def __ge__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__ge__, shapes.symmetric, self, other)

    def __lt__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__lt__, shapes.symmetric, self, other)

    def __le__(self, other: TensorLike) -> TensorLike:
        return lazy_forward(Tensor.__le__, shapes.symmetric, self, other)

    def __len__(self) -> int:
        return self.size(0)

    def __getitem__(
        self, index: int | slice | Tensor | List[Any] | Tuple[Any] | None
    ) -> Tensor:
        if isinstance(self._data, RunnableTensor):
            data = self._data.run()
        else:
            data = self._data
        return data[index]

    def __setitem__(
        self,
        index: int | slice | Tensor | List[Any] | Tuple[Any] | None,
        value: Tensor,
    ) -> None:
        if isinstance(self._data, RunnableTensor):
            raise UnsupportedError

        self._data[index] = value

    def __getattr__(self, name: str) -> Callable[..., Any]:
        logger.debug("__getattr__ called. Automatically resolving function.")

        method = getattr(Tensor, name)
        wrapper = functools.wraps(method)

        if (custom_impl := CUSTOM_OPS.lookup_method(name)) is not None:
            logger.debug("A custom method definition is found.")
            partial = functools.partial(custom_impl, self)
        elif (shape_impl := SHAPE_OPS.lookup_method(name)) is not None:
            logger.debug("A custom shape method is found. Lazy evaluation.")
            partial = functools.partial(lazy_forward, method, shape_impl, self)
        else:
            logger.debug("No custom methods found. Evaluating eagerly.")
            partial = functools.partial(method, run(self))

        return wrapper(partial)

    @classmethod
    def __torch_function__(
        cls,
        func: Callable[..., Tensor],
        types: Tuple[Type[Any], ...],
        args: Sequence[TensorLike] = (),
        kwargs: Dict[str, TensorLike] | None = None,
    ) -> TensorLike:
        if kwargs is None:
            kwargs = {}

        if not builtins.all(
            issubclass(typ, (LazyTensor, Tensor, int, float, bool)) for typ in types
        ):
            return NotImplemented

        name = func.__name__

        if (custom_impl := CUSTOM_OPS.lookup_function(name)) is not None:
            logger.debug("A custom function definition is found.")
            return custom_impl(*args, **kwargs)
        elif (shape_impl := SHAPE_OPS.lookup_function(name)) is not None:
            logger.debug("A custom shape function is found. Lazy evaluation.")
            return lazy_forward(func, shape_impl, *args, **kwargs)
        else:
            logger.debug("No custom method found. Evaluating eagerly.")
            args = [run(arg) for arg in args]
            kwargs = {k: run(v) for (k, v) in kwargs.items()}
            return func(*args, **kwargs)

    @property
    @wraps(Tensor.size)
    def shape(self) -> Tuple[int, ...]:
        return self.size()

    @property
    @wraps(Tensor.dim)
    def ndim(self) -> int:
        return self.dim()

    @property
    @wraps(Tensor.t)
    def T(self) -> TensorLike:
        return self.t()

    def torch(self) -> Tensor:
        return self.run()

    def backward(self) -> None:
        self.run().backward()


@overload
def lazy(val: Tensor, batch: int | None = None) -> LazyTensor:
    ...


@overload
def lazy(val: LazyTensor) -> LazyTensor:
    ...


@overload
def lazy(val: int) -> int:
    ...


@overload
def lazy(val: float) -> float:
    ...


@overload
def lazy(val: bool) -> bool:
    ...


def lazy(val: Any, batch: int | None = None) -> Any:
    if isinstance(val, LazyTensor):
        return val

    if isinstance(val, Tensor):
        return LazyTensor(val, batch)

    return val


@overload
def run(val: LazyTensor, partial: PartialInfo | None = None) -> Tensor:
    ...


@overload
def run(val: RunnableTensor, partial: PartialInfo | None = None) -> Tensor:
    ...


@overload
def run(val: Runnable[T], partial: PartialInfo | None = None) -> T:
    ...


@overload
def run(val: T, partial: PartialInfo | None = None) -> T:
    ...


def run(val: Any, partial: PartialInfo | None = None) -> Any:
    if isinstance(val, Runnable):
        return val.run(partial)

    return val


def lazy_forward(
    func: Callable[..., Any], shape_func: ShapeFunction, *args: Any, **kwargs: Any
) -> TensorLike:
    if torch.is_grad_enabled():
        return LazyTensor(LazyFunction(func, shape_func)(*args, **kwargs))
    else:
        return func(*args, **kwargs)


# Functions that require special handling.


class _ValIdx(NamedTuple):
    values: TensorLike
    indices: TensorLike


@overload
def _min(input: TensorLike) -> TensorLike:
    ...


@overload
def _min(input: TensorLike, dim: int, keepdim: bool = False) -> _ValIdx:
    ...


@overload
def _min(input: TensorLike, other: TensorLike) -> TensorLike:
    ...


@wraps(torch.min)
def _min(input: TensorLike, *args: Any, **kwargs: Any) -> TensorLike | _ValIdx:
    if len(args) == len(kwargs) == 0:
        return lazy_forward(torch.min, shapes.reduce_dims, input)

    if (
        len(args) == 1
        and isinstance((other := args[0]), (Tensor, LazyTensor))
        or len(kwargs) == 1
        and (other := kwargs.get("other", None) is not None)
    ):
        return lazy_forward(torch.minimum, shapes.symmetric, input, other)

    return _ValIdx(
        lazy_forward(torch.amin, shapes.reduce_dims, input, *args, **kwargs),
        lazy_forward(torch.argmin, shapes.reduce_dims, input, *args, **kwargs),
    )


@overload
def _max(input: TensorLike) -> TensorLike:
    ...


@overload
def _max(input: TensorLike, dim: int, keepdim: bool = False) -> _ValIdx:
    ...


@overload
def _max(input: TensorLike, other: TensorLike) -> TensorLike:
    ...


@wraps(torch.max)
def _max(input: TensorLike, *args: Any, **kwargs: Any) -> TensorLike | _ValIdx:
    if len(args) == len(kwargs) == 0:
        return lazy_forward(torch.max, shapes.reduce_dims, input)

    if (
        len(args) == 1
        and isinstance((other := args[0]), (Tensor, LazyTensor))
        or len(kwargs) == 1
        and (other := kwargs.get("other", None) is not None)
    ):
        return lazy_forward(torch.maximum, shapes.symmetric, input, other)

    return _ValIdx(
        lazy_forward(torch.amax, shapes.reduce_dims, input, *args, **kwargs),
        lazy_forward(torch.argmax, shapes.reduce_dims, input, *args, **kwargs),
    )


def _permute_function_shape(
    input: TensorLike, dims: int | Tuple[int, ...], *args: Any, **kwargs: Any
) -> Shape:
    shapes.mute_unused_args(*args, **kwargs)

    if isinstance(dims, int):
        dims = (dims,)

    return shapes.permute(input, *dims)


def _t_shape(input: TensorLike, *args: Any, **kwargs: Any) -> Shape:
    shapes.mute_unused_args(*args, **kwargs)

    return shapes.tranpose(input, 0, 1)


@dataclass
class MethodFunction(Generic[T]):
    method: Dict[str, T]
    function: Dict[str, T]

    @staticmethod
    def _search(key: str, *dbs: Dict[str, T]) -> T | None:
        for db in dbs:
            if (value := db.get(key)) is not None:
                return value
        return None

    def lookup(self, key: str, *dbs: Dict[str, T]) -> T | None:
        if (result := self._search(key, *dbs)) is not None:
            return result

        fallback = key.lstrip("_")
        return self._search(fallback, *dbs)

    def lookup_method(self, key: str) -> T | None:
        return self.lookup(key, self.method, self.function)

    def lookup_function(self, key: str) -> T | None:
        return self.lookup(key, self.function)


CUSTOM_OPS = MethodFunction[Callable](
    method={},
    function={
        "min": _min,
        "max": _max,
    },
)

PARTIAL_OPS = MethodFunction[Callable](method={}, function={"sum": lambda x: x})

SHAPE_OPS = MethodFunction[ShapeFunction](
    method={"permute": shapes.permute},
    function={
        "positive": shapes.identity,
        "negative": shapes.identity,
        "neg": shapes.identity,
        "add": shapes.symmetric,
        "sub": shapes.symmetric,
        "subtract": shapes.symmetric,
        "mul": shapes.symmetric,
        "multiply": shapes.symmetric,
        "div": shapes.symmetric,
        "divide": shapes.symmetric,
        "true_divide": shapes.symmetric,
        "floor": shapes.identity,
        "fmod": shapes.symmetric,
        "remainder": shapes.symmetric,
        "frac": shapes.identity,
        "pow": shapes.symmetric,
        "exp": shapes.identity,
        "exp2": shapes.identity,
        "log": shapes.identity,
        "log2": shapes.identity,
        "log10": shapes.identity,
        "log1p": shapes.identity,
        "abs": shapes.identity,
        "matmul": shapes.matmul,
        "bmm": shapes.matmul,
        "mm": shapes.matmul,
        "mv": shapes.matmul,
        "dot": shapes.matmul,
        "eq": shapes.symmetric,
        "equal": shapes.symmetric,
        "ne": shapes.symmetric,
        "not_equal": shapes.symmetric,
        "gt": shapes.symmetric,
        "greater": shapes.symmetric,
        "ge": shapes.symmetric,
        "greater_equal": shapes.symmetric,
        "lt": shapes.symmetric,
        "less": shapes.symmetric,
        "le": shapes.symmetric,
        "less_equal": shapes.symmetric,
        "mean": shapes.reduce_dims,
        "sum": shapes.reduce_dims,
        "std": shapes.reduce_dims,
        "minimum": shapes.symmetric,
        "maximum": shapes.symmetric,
        "amin": shapes.reduce_dims,
        "amax": shapes.reduce_dims,
        "argmin": shapes.reduce_dims,
        "argmax": shapes.reduce_dims,
        "isclose": shapes.symmetric,
        "allclose": shapes.reduce_dims,
        "cat": shapes.cat,
        "t": _t_shape,
        "permute": _permute_function_shape,
        "transpose": shapes.tranpose,
        "select": shapes.select,
        "index_select": shapes.select,
        "sin": shapes.identity,
        "cos": shapes.identity,
        "tan": shapes.identity,
        "asin": shapes.identity,
        "acos": shapes.identity,
        "atan": shapes.identity,
        "sinh": shapes.identity,
        "cosh": shapes.identity,
        "tanh": shapes.identity,
        "asinh": shapes.identity,
        "acosh": shapes.identity,
        "atanh": shapes.identity,
        "sigmoid": shapes.identity,
        "hardsigmoid": shapes.identity,
        "softmax": shapes.identity,
        "relu": shapes.identity,
        "relu6": shapes.identity,
        "leaky_relu": shapes.identity,
        "binary_cross_entropy": shapes.reduce_dims,
        "binary_cross_entropy_with_logits": shapes.reduce_dims,
        "elu": shapes.identity,
        "gelu": shapes.identity,
        "dropout": shapes.identity,
        "batch_norm": shapes.identity,
        "layer_norm": shapes.identity,
        "linear": shapes.linear,
        "embedding": shapes.embedding,
        "pad": shapes.pad,
        "conv1d": shapes.conv,
        "conv2d": shapes.conv,
        "conv3d": shapes.conv,
        "conv_transpose1d": shapes.conv_transpose,
        "conv_transpose2d": shapes.conv_transpose,
        "conv_transpose3d": shapes.conv_transpose,
        "max_pool1d": shapes.maxpool,
        "max_pool2d": shapes.maxpool,
        "max_pool3d": shapes.maxpool,
        "avg_pool1d": shapes.avgpool,
        "avg_pool2d": shapes.avgpool,
        "avg_pool3d": shapes.avgpool,
        # Functions that will not be implemented.
        "__floordiv__": NeverImplementedError.raise_error,
    },
)
