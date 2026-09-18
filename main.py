from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import time
from datetime import datetime, timezone

import os
import psycopg2

COLLATERAL_TOKENS = {
    "USDC (native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "USDC.e (bridged, legacy)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "pUSD (current)": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
}

EXCHANGE_CONTRACTS = {
    "CTF Exchange (v1)": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange (v1)": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "CTF Exchange V2": "0xE111180000d2663C0091e4f400237545B87B996B",
    "Neg Risk CTF Exchange V2": "0xe2222d279d744050d28e00520010520000310F59",
}

CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

ERC20_TRANSFER_ABI = [{
    "anonymous": False,
    "name": "Transfer",
    "type": "event",
    "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False},
    ],
}]

ORDER_FILLED_ABI = [{
    "anonymous": False,
    "name": "OrderFilled",
    "type": "event",
    "inputs": [
        {"name": "orderHash", "type": "bytes32", "indexed": True},
        {"name": "maker", "type": "address", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "makerAssetId", "type": "uint256", "indexed": False},
        {"name": "takerAssetId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "fee", "type": "uint256", "indexed": False},
    ],
}]

CTF_EVENTS_ABI = [
    {
        "anonymous": False, "name": "PositionSplit", "type": "event",
        "inputs": [
            {"name": "stakeholder", "type": "address", "indexed": True},
            {"name": "collateralToken", "type": "address", "indexed": False},
            {"name": "parentCollectionId", "type": "bytes32", "indexed": True},
            {"name": "conditionId", "type": "bytes32", "indexed": True},
            {"name": "partition", "type": "uint256[]", "indexed": False},
            {"name": "amount", "type": "uint256", "indexed": False},
        ],
    },
    {
        "anonymous": False, "name": "PositionsMerge", "type": "event",
        "inputs": [
            {"name": "stakeholder", "type": "address", "indexed": True},
            {"name": "collateralToken", "type": "address", "indexed": False},
            {"name": "parentCollectionId", "type": "bytes32", "indexed": True},
            {"name": "conditionId", "type": "bytes32", "indexed": True},
            {"name": "partition", "type": "uint256[]", "indexed": False},
            {"name": "amount", "type": "uint256", "indexed": False},
        ],
    },
    {
        "anonymous": False, "name": "PayoutRedemption", "type": "event",
        "inputs": [
            {"name": "redeemer", "type": "address", "indexed": True},
            {"name": "collateralToken", "type": "address", "indexed": False},
            {"name": "parentCollectionId", "type": "bytes32", "indexed": True},
            {"name": "conditionId", "type": "bytes32", "indexed": True},
            {"name": "indexSets", "type": "uint256[]", "indexed": False},
            {"name": "payout", "type": "uint256", "indexed": False},
        ],
    },
]

DB_CONFIG = {
    "host": "localhost",
    "port": "5432",
    "dbname": "polymarket",
    "user": "polymarket",
    "password": "polymarket",
}


"""
1) Fetch transactions w/ Polygon RPC
2) Store them in PostgreSQL
3) Calculate current balance

https://polycopy.app/trader/0x46b353667fd7d846af3bbeda6584b0e5b883d3de
https://polymarket.com/@bjprolo
"""

# DB helpers

def initDB():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id            SERIAL PRIMARY KEY,
                wallet        TEXT NOT NULL,
                record_type   TEXT NOT NULL,
                block_number  BIGINT NOT NULL,
                tx_hash       TEXT NOT NULL,
                contract      TEXT,
                counterparty  TEXT,
                amount        NUMERIC,
                amount_raw    NUMERIC,
                detail        TEXT,
                event_time    TIMESTAMPTZ,
                inserted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    conn.commit()
    print("Init postgreSQL")
    return conn

def insertRecordIntoDB(conn, wallet, record):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO records
            (
                wallet,
                record_type,
                block_number,
                tx_hash,
                contract,
                counterparty,
                amount,
                amount_raw,
                detail,
                event_time
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                wallet,
                record.get("type"),
                record.get("block"),
                record.get("tx_hash"),
                record.get("contract"),
                record.get("counterparty"),
                record.get("amount"),
                record.get("amount_raw"),
                record.get("detail"),
                record.get("timestamp"),
            ),
        )
    conn.commit()
    print("Inserted record into DB")

def getBalanceFromDB(conn, wallet):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(amount) FILTER (WHERE record_type = 'deposit'), 0)
                - COALESCE(SUM(amount) FILTER (WHERE record_type = 'withdrawal'), 0)
                AS net_collateral
            FROM records
            WHERE wallet = %s
            """,
            (wallet,),
        )
        (balance,) = cur.fetchone()
    print(f"Balance record from DB: {balance}")
    return balance

def blockTimestamp(w3, cache, blockNumber):
    if blockNumber not in cache:
        cache[blockNumber] = w3.eth.get_block(blockNumber).timestamp
    return datetime.fromtimestamp(cache[blockNumber], tz=timezone.utc)

def addTimestamps(w3, rows):
    cache = {}
    for row in rows:
        row["timestamp"] = blockTimestamp(w3, cache, row["block"]).isoformat()
    return rows

# Fetching records helpers

def connect(rpcUrl: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpcUrl))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

def scanEvent(w3, contract, eventName, argumentFilters, fromBlock, toBlock):
    event = getattr(contract.events, eventName)
    results = []
    block = fromBlock
    while block <= toBlock:
        end = min(block + 3000 - 1, toBlock)
        attempts = 0
        while True:
            try:
                logs = event.get_logs(
                    argument_filters=argumentFilters,
                    from_block=block,
                    to_block=end,
                )
                break
            except Exception as exc:
                body = getattr(
                    getattr(exc, "response", None),
                    "text",
                    None
                ) or str(exc)

                print(f"[{block}-{end}] {body}")

                attempts += 1
                if attempts > 6:
                    raise

                if end > block:
                    end = block + max((end - block) // 2, 0)

                time.sleep(2.0)

        results.extend(logs)
        block = end + 1
        print(block)

    return results

# Fetching records
def fetchDepositsWithdrawals(w3, wallet, fromBlock, toBlock):
    rows = []
    for tokenLabel, tokenAddr in COLLATERAL_TOKENS.items():
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(tokenAddr),
            abi=ERC20_TRANSFER_ABI
        )
        for direction, argFilter in (
            ("out", {"from": wallet}),
            ("in", {"to": wallet})
        ):
            logs = scanEvent(
                w3,
                contract,
                "Transfer",
                argFilter,
                fromBlock,
                toBlock
            )
            print("fetchDepositsWithdrawals scanned events into logs")
            for log in logs:
                a = log["args"]
                rows.append({
                    "type": "withdrawal" if direction == "out" else "deposit",
                    "block": log["blockNumber"],
                    "tx_hash": log["transactionHash"].hex(),
                    "contract": tokenLabel,
                    "counterparty": a["to"] if direction == "out" else a["from"],
                    "amount_raw": a["value"],
                    "amount": a["value"] / 1e6,
                    "detail": "",
                })
        print(f"{tokenLabel}: done")
    return rows
 
def fetchTrades(w3, wallet, fromBlock, toBlock):
    rows = []
    for exchLabel, exchAddr in EXCHANGE_CONTRACTS.items():
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(exchAddr),
            abi=ORDER_FILLED_ABI
        )

        for role, argFilter in (
            ("maker", {"maker": wallet}),
            ("taker", {"taker": wallet})
        ):
            logs = scanEvent(
                w3,
                contract,
                "OrderFilled",
                argFilter,
                fromBlock,
                toBlock
            )

            for log in logs:
                a = log["args"]
                rows.append({
                    "type": f"trade ({role})",
                    "block": log["blockNumber"],
                    "tx_hash": log["transactionHash"].hex(),
                    "contract": exchLabel,
                    "counterparty": a["taker"] if role == "maker" else a["maker"],
                    "amount_raw": a["makerAmountFilled"] if role == "maker" else a["takerAmountFilled"],
                    "amount": (a["makerAmountFilled"] if role == "maker" else a["takerAmountFilled"]) / 1e6,
                    "detail": (f"makerAssetId={a['makerAssetId']} takerAssetId={a['takerAssetId']} "
                               f"fee={a['fee']}"),
                })
        print(f"{exchLabel}: done")
    return rows
 
def fetchCtfEvents(w3, wallet, fromBlock, toBlock):
    rows = []
    ctfContract = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS),
        abi=CTF_EVENTS_ABI
    )
    for eventName, walletArg in (
        ("PositionSplit", "stakeholder"),
        ("PositionsMerge", "stakeholder"),
        ("PayoutRedemption", "redeemer"),
    ):
        logs = scanEvent(
            w3,
            ctfContract,
            eventName,
            {walletArg: wallet},
            fromBlock,
            toBlock
        )
        for log in logs:
            a = log["args"]
            amount = a.get("amount", a.get("payout", 0))
            rows.append({
                "type": eventName,
                "block": log["blockNumber"],
                "tx_hash": log["transactionHash"].hex(),
                "contract": "Conditional Tokens",
                "counterparty": "",
                "amount_raw": amount,
                "amount": amount / 1e6,
                "detail": f"conditionId={a['conditionId'].hex()}",
            })
    print("Conditional Tokens (splits/merges/redemptions): done")
    return rows

def main():
    w3 = connect("https://polygon.drpc.org")
    wallet = Web3.to_checksum_address("0x46b353667fd7d846af3bbeda6584b0e5b883d3de")
    latestBlock = w3.eth.block_number
    # Free tier only allows for a limited amount of blocks to access
    startBlock = latestBlock - 100
    
    conn = initDB()

    print("Starting to fetch all records...")
    polymarketWalletRecords = []
    polymarketWalletRecords += fetchDepositsWithdrawals(w3, wallet, startBlock, latestBlock)
    polymarketWalletRecords += fetchTrades(w3, wallet, startBlock, latestBlock)
    polymarketWalletRecords += fetchCtfEvents(w3, wallet, startBlock, latestBlock)

    addTimestamps(w3, polymarketWalletRecords)
    polymarketWalletRecords.sort(key=lambda r: r["block"])

    for record in polymarketWalletRecords:
        insertRecordIntoDB(conn, wallet, record)
        print(record)

    balance = getBalanceFromDB(conn, wallet)
    print(f"Balance: {balance}")

if __name__ == "__main__":
    main()
