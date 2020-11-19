"""
The tx command is a tool to create "raw" transactions

create_unsigned_transaction - take a list of transaction creation descriptions,
and produce their SpendBundles, optionally verifying them.

"""
from typing import List

# tx command imports
import json
import asyncio
from clvm_tools import binutils
from src.util.byte_types import hexstr_to_bytes
from src.util.chech32 import decode_puzzle_hash

# blspy
from blspy import G1Element, G2Element

# transaction imports
from src.types.program import Program
from src.types.sized_bytes import bytes32
from src.types.coin_solution import CoinSolution
from src.types.spend_bundle import SpendBundle
from src.types.coin import Coin
from src.util.ints import uint64, uint16
from src.wallet.puzzles.puzzle_utils import (
    make_assert_my_coin_id_condition,
    make_assert_time_exceeds_condition,
    make_assert_coin_consumed_condition,
    make_create_coin_condition,
    make_assert_fee_condition,
)
from src.wallet.puzzles.p2_delegated_puzzle import (
    puzzle_for_pk,
    solution_for_conditions,
)
from src.util.debug_spend_bundle import debug_spend_bundle

# Connect to actual wallet
from src.rpc.wallet_rpc_client import WalletRpcClient


# TODO: From wallet/wallet.py: refactor
def make_solution(primaries=None, min_time=0, me=None, consumed=None, fee=0):
    assert fee >= 0
    condition_list = []
    if primaries:
        for primary in primaries:
            condition_list.append(
                make_create_coin_condition(primary["puzzlehash"], primary["amount"])
            )
    if consumed:
        for coin in consumed:
            condition_list.append(make_assert_coin_consumed_condition(coin))
    if min_time > 0:
        condition_list.append(make_assert_time_exceeds_condition(min_time))
    if me:
        condition_list.append(make_assert_my_coin_id_condition(me["id"]))
    if fee:
        condition_list.append(make_assert_fee_condition(fee))
    print(condition_list)
    return solution_for_conditions(condition_list)


class SpendRequest:
    puzzle_hash: bytes32
    amount: uint64

    def __init__(self, ph, amt):
        self.puzzle_hash = ph
        self.amount = amt


def create_unsigned_transaction(
    inputs: List[dict] = None,
    spend_requests: List[SpendRequest] = None,
    validate=True,
) -> List[CoinSolution]:
    """
    Generates a unsigned transaction in form of List(Puzzle, Solutions)
    """

    if inputs is None or len(inputs) < 1:
        raise ValueError("tx create requires one or more input_coins")
    assert len(inputs) > 0
    # We treat the first coin as the origin
    # For simplicity, only the origin coin creates outputs
    origin = inputs.pop()
    outputs = []
    input_value = sum([i["coin"].amount for i in inputs]) + origin["coin"].amount
    sent_value = 0
    if spend_requests is not None:
        sent_value = sum([req.amount for req in spend_requests])
        for request in spend_requests:
            outputs.append({"puzzlehash": request.puzzle_hash, "amount": request.amount})
    if validate and sent_value >= input_value:
        raise (
            ValueError(
                f"input amounts ({input_value}) are less than outputs ({sent_value})"
            )
        )

    spends: List[CoinSolution] = []

    # Eventually, we will support specifying the puzzle directly in the
    # input to create_unsigned_transaction (viaCoinWithPuzzle).
    # For now, we specify a pubkey, and use the "standard transaction"
    origin_puzzle = puzzle_for_pk(origin["pubkey"])
    assert origin_puzzle.get_tree_hash() == origin["coin"].puzzle_hash

    solution = make_solution(primaries=outputs, fee=0)
    puzzle_solution_pair = Program.to([origin_puzzle, solution])
    spends.append(CoinSolution(origin["coin"], puzzle_solution_pair))

    for i in inputs:
        coin = i["coin"]
        print(f"processing coin {coin}")
        solution = make_solution()
        puzzle = puzzle_for_pk(coin.pubkey)
        puzzle_solution_pair = Program.to([puzzle, solution])
        spends.append(CoinSolution(coin, puzzle_solution_pair))

    return spends


def create_unsigned_tx_from_json(json_tx) -> SpendBundle:
    j = json.loads(json_tx)
    spends = []
    for s in j["spends"]:
        if "spend_requests" in s:
            input_coins_json = s["input_coins"]
            spend_requests_json = s["spend_requests"]  # Output addresses and amounts
            input_coins = [
                {
                    "coin": Coin(
                        hexstr_to_bytes(i["coin"]["parent_id"]),
                        hexstr_to_bytes(i["coin"]["puzzle_hash"]),
                        i["coin"]["amount"]
                    ),
                    "pubkey": G1Element.from_bytes(hexstr_to_bytes(i["pubkey"]))
                }
                for i in input_coins_json
            ]
            spend_requests = [
                SpendRequest(hexstr_to_bytes(s["puzzle_hash"]), s["amount"])
                for s in spend_requests_json
            ]
            print(input_coins, spend_requests)
            spends.extend(create_unsigned_transaction(input_coins, spend_requests))
        elif "solution" in s:
            input_coin = Coin(
                hexstr_to_bytes(s["input_coin"]["parent_id"]),
                hexstr_to_bytes(s["input_coin"]["puzzle_hash"]),
                uint64(s["input_coin"]["amount"]),
            )
            puzzle_reveal = Program(binutils.assemble(s["puzzle_reveal"]))
            assert puzzle_reveal.get_tree_hash() == input_coin.puzzle_hash
            solution = Program(binutils.assemble(s["solution"]))
            spends.append(
                CoinSolution(input_coin, Program.to([puzzle_reveal, solution]))
            )

    spend_bundle = SpendBundle(spends, G2Element.infinity())
    return spend_bundle
    # output = { "spends": spends }

    # TODO: Object of type CoinSolution is not JSON serializable
    # print(json.dumps(output))


# Command line handling

command_list = [
    "create",
    "verify",
    "sign",
    "push",
    "encode",
    "decode",
    "view-coins",
    "get-address",
]


def help_message():
    print(
        "usage: chia tx command\n"
        + f"command can be any of {command_list}\n"
        + "\n"
        + "Examples:\n"
        + "    chia tx create json_transaction_specification\n"
        + "    chia tx push json_transaction\n"
        + "    chia tx view-coins\n"
        + "    chia tx encode spend_bundle_json\n"
        + "    chia tx decode spend_bundle_hex_bytes\n"
    )


def encode_spendbundle(spend_bundle: SpendBundle):
    return bytes(spend_bundle).hex()


def decode_spendbundle(spend_bundle_bytes: str):
    return SpendBundle.from_bytes(bytes.fromhex(spend_bundle_bytes))


def make_parser(parser):
    parser.add_argument(
        "command",
        help=f"Command can be any one of {command_list}",
        type=str
    )
    parser.add_argument(
        "cmd_args",
        nargs="*"
    )

    parser.set_defaults(function=handler)
    parser.print_help = lambda self=parser: help_message()


async def get_new_address():
    wrpc = await WalletRpcClient.create("127.0.0.1", uint16(9256))
    address = await wrpc.get_next_address(1)
    print(f"Chech32 encoded: {address}")
    print(f"Puzzlehash: {decode_puzzle_hash(address).hex()}")
    wrpc.close()


async def push_spendbundle(spend_bundle: SpendBundle):
    wrpc = await WalletRpcClient.create("127.0.0.1", uint16(9256))
    await wrpc.push_spend_bundle(bytes(spend_bundle).hex())
    wrpc.close()
    return


async def view_coins(args):
    wrpc = await WalletRpcClient.create("127.0.0.1", uint16(9256))
    coins = await wrpc.get_spendable_coins(1)
    print()
    for coin in coins:
        print(coin)
        print(binutils.disassemble(Program.from_bytes(bytes.fromhex(coin["puzzle"]))))
        print()
    wrpc.close()
    return


def fail_cmd(parser, msg):
    print(f"\n{msg}")
    help_message()
    parser.exit(1)


async def sign_spendbundle(spend_bundle) -> SpendBundle:
    wrpc = await WalletRpcClient.create("127.0.0.1", uint16(9256))
    signed_spend_bundle: str = await wrpc.sign_spend_bundle(bytes(spend_bundle).hex())
    debug_spend_bundle(SpendBundle.from_bytes(bytes.fromhex(signed_spend_bundle)))
    return SpendBundle.from_bytes(bytes.fromhex(signed_spend_bundle))


def handler(args, parser):

    command = args.command
    if command not in command_list:
        help_message()
        parser.exit(1)

    if args.cmd_args is None or len(args.cmd_args) < 1:
        fail_cmd(parser, f"Too few arguments to command 'chia tx {command}'")
    if len(args.cmd_args) > 1:
        fail_cmd(parser, f"Too many arguments to command 'chia tx {command}'")

    if command == "create":
        json_tx = args.cmd_args[0]
        spend_bundle = create_unsigned_tx_from_json(json_tx)
        debug_spend_bundle(spend_bundle)
        print(bytes(spend_bundle).hex())
    elif command == "verify":
        print()
    elif command == "sign":
        json_tx = args.cmd_args[0]
        spend_bundle = create_unsigned_tx_from_json(json_tx)
        signed_sb: SpendBundle = asyncio.get_event_loop().run_until_complete(sign_spendbundle(spend_bundle))
        debug_spend_bundle(signed_sb)
    elif command == "push":
        json_tx = args.cmd_args[0]
        spend_bundle = create_unsigned_tx_from_json(json_tx)
        signed_sb: SpendBundle = asyncio.get_event_loop().run_until_complete(sign_spendbundle(spend_bundle))
        debug_spend_bundle(signed_sb)
        return asyncio.get_event_loop().run_until_complete(push_spendbundle(signed_sb))
    elif command == "encode":
        pass
    elif command == "decode":
        sb = decode_spendbundle(args.cmd_args[0])
        debug_spend_bundle(sb)
        print(sb)
    elif command == "view-coins":
        parser.exit(asyncio.get_event_loop().run_until_complete(view_coins(args.cmd_args)))
    else:
        print(f"command '{command}' is not recognised")
        parser.exit(1)
