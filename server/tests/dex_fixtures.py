"""Canonical generic V6 transaction fixtures for executor control-flow tests."""
from eth_abi import encode
from eth_utils import keccak

ROUTER = "0x111111125421ca6dc452d289314280a0f8842a65"
WALLET = "0x" + "11" * 20
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
EXECUTOR = "0x" + "44" * 20


def generic_swap_payload(src=USDC, dst=WETH, amount=100_000_000,
                         output=40_000_000_000_000_000, wallet=WALLET):
    signature = "swap(address,(address,address,address,address,uint256,uint256,uint256),bytes)"
    data = keccak(text=signature)[:4] + encode(
        ("address", "(address,address,address,address,uint256,uint256,uint256)", "bytes"),
        (EXECUTOR, (src, dst, EXECUTOR, wallet, amount, output, 0), b"route"),
    )
    return dict(chainId=8453, to=ROUTER, data="0x" + data.hex(), value=0,
                nonce=0, gas=300_000, gasPrice=1_000_000)


def approval_payload(token=USDC, amount=250_000_000):
    return dict(chainId=8453, to=token, value=0, nonce=0, gas=60_000, gasPrice=1_000_000,
                data="0x095ea7b3" + encode(("address", "uint256"), (ROUTER, amount)).hex())
