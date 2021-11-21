import torch

from koila.core import LazyTensor


def test_add() -> None:
    a = LazyTensor(torch.tensor(1))
    b = LazyTensor(torch.tensor(2))
    c = a + b
    assert c.run().item() == 3
