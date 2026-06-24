Audit Report

## Title
ckERC20 Minter Finalizes Withdrawals Based Solely on EVM Receipt Status, Missing ERC-20 Transfer Verification — (`File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

## Summary

The ckERC20 minter's withdrawal finalization logic checks only the EVM-level transaction receipt `status` field to determine success or failure. Because the minter's `TransactionReceipt` struct stores no `logs` field and the `From<EvmTransactionReceipt>` conversion discards all log data, the minter has no structural ability to verify that an ERC-20 `Transfer` event was actually emitted. For any ERC-20 token whose `transfer()` returns `false` without reverting — a behavior explicitly permitted by the ERC-20 standard — the minter permanently burns the user's ckERC20 tokens on the IC ledger while no underlying ERC-20 tokens are delivered, and simultaneously understates its internal ERC-20 balance.

## Finding Description

**Transaction construction** encodes `transfer(to, amount)` with selector `0xa9059cbb` into the `data` field of an EIP-1559 transaction sent to the ERC-20 contract address: [1](#0-0) 

**Finalization** in `record_finalized_transaction` gates reimbursement exclusively on `receipt.status == TransactionStatus::Failure`. For `CkErc20` withdrawals, if the receipt status is `Success`, no reimbursement is ever scheduled: [2](#0-1) 

**Balance accounting** in `update_balance_upon_withdrawal` calls `erc20_balances.erc20_sub()` whenever `receipt.status == TransactionStatus::Success` and the transaction has non-empty calldata: [3](#0-2) 

**Root cause — structural impossibility of log inspection**: The `TransactionReceipt` struct contains no `logs` field: [4](#0-3) 

The `From<EvmTransactionReceipt>` conversion explicitly discards all log data from the EVM RPC response, mapping only `block_hash`, `block_number`, `effective_gas_price`, `gas_used`, `status`, and `transaction_hash`: [5](#0-4) 

`TransactionCallData::decode` only parses input calldata; there is no mechanism to inspect the return value of the `transfer()` call: [6](#0-5) 

The EVM receipt `status = 0x1` only reflects that the outermost call frame did not revert. An ERC-20 `transfer()` that returns `false` without reverting produces `status = 0x1` and emits no `Transfer(from, to, value)` event. The minter cannot distinguish this from a genuinely successful transfer.

## Impact Explanation

This is a chain-fusion ledger conservation bug matching the Critical impact class: **permanent loss of in-scope chain-key/ledger assets**. When triggered:

1. The user's ckERC20 tokens are burned on the IC ledger (irreversible).
2. No ERC-20 tokens leave the minter's Ethereum address.
3. `erc20_balances.erc20_sub()` reduces the minter's internal accounting for that token, understating its balance.
4. The understatement corrupts future withdrawal accounting for all users of that token, potentially enabling over-withdrawal by subsequent users.

The combination of permanent ckERC20 burn with no ERC-20 delivery, plus corrupted internal balance state, constitutes protocol insolvency for the affected token.

## Likelihood Explanation

Currently deployed ckERC20 tokens (ckUSDC, ckUSDT) revert on failure, so the bug is not immediately exploitable on mainnet. However:

- The ERC-20 standard (EIP-20) explicitly permits returning `false` instead of reverting; many production tokens use this pattern.
- Any future ckERC20 token added via NNS governance whose `transfer()` returns `false` under any condition (blacklisted recipient, paused state, insufficient balance edge case) immediately exposes all withdrawal users to fund loss.
- No attacker capability is required beyond initiating a normal `withdraw_erc20` call — the exploit path is fully unprivileged.
- The bug is latent and deterministic: it will trigger on every withdrawal to a recipient that causes the token's `transfer()` to return `false`.

## Recommendation

After retrieving the transaction receipt, verify that the ERC-20 transfer actually succeeded by checking `receipt.logs` for a `Transfer(indexed address from, indexed address to, uint256 value)` event matching the minter's Ethereum address as `from`, the withdrawal destination as `to`, and the withdrawal amount as `value`. If the `Transfer` event is absent despite `receipt.status == Success`, treat the withdrawal as failed and schedule reimbursement identically to the `Failure` branch.

This requires:
1. Adding a `logs` field to `TransactionReceipt` and populating it in the `From<EvmTransactionReceipt>` conversion.
2. Implementing a `Transfer` event log parser (topic0 = `keccak256("Transfer(address,address,uint256)")`, topic1 = from, topic2 = to, data = value).
3. In `record_finalized_transaction` for `WithdrawalRequest::CkErc20`, adding a check: if `receipt.status == Success` but no matching `Transfer` log is present, call `record_reimbursement_request` and skip `erc20_sub`.

## Proof of Concept

1. Deploy a test ERC-20 token whose `transfer()` returns `false` (without reverting) when the recipient is on a blocklist.
2. Add this token as a ckERC20 token on a local replica/PocketIC instance.
3. Mint ckERC20 tokens to a test user.
4. Call `withdraw_erc20` specifying a blocklisted Ethereum address as recipient.
5. The minter burns the ckERC20 tokens and submits the Ethereum transaction.
6. The ERC-20 `transfer()` executes, returns `false`, emits no `Transfer` event, does not revert; receipt `status = 0x1`.
7. `record_finalized_transaction` observes `receipt.status != Failure` → no reimbursement scheduled.
8. `update_balance_upon_withdrawal` observes `receipt.status == Success` → calls `erc20_balances.erc20_sub(token, amount)`.
9. Assert: user's ckERC20 balance is zero; user received no ERC-20 tokens; minter's internal ERC-20 balance is understated by `amount`.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-746)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
            }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1183)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
                gas_limit,
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1211-1232)
```rust
    pub fn decode<T: AsRef<[u8]>>(data: T) -> Result<Self, String> {
        let data = data.as_ref();
        match data.get(0..4) {
            Some(selector) if selector == ERC_20_TRANSFER_FUNCTION_SELECTOR => {
                if data.len() != 68 {
                    return Err("Invalid data length".to_string());
                }
                let address = <[u8; 32]>::try_from(&data[4..36]).unwrap();
                let to = Address::try_from(&address).unwrap();

                let value = <[u8; 32]>::try_from(&data[36..]).unwrap();
                let value = Erc20Value::from_be_bytes(value);

                Ok(TransactionCallData::Erc20Transfer { to, value })
            }
            Some(selector) => Err(format!(
                "Unknown function selector 0x{:?}",
                hex::encode(selector)
            )),
            None => Err("missing function selector".to_string()),
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L377-383)
```rust
        if receipt.status == TransactionStatus::Success && !tx.transaction_data().is_empty() {
            let TransactionCallData::Erc20Transfer { to: _, value } = TransactionCallData::decode(
                tx.transaction_data(),
            )
            .expect("BUG: failed to decode transaction data from transaction issued by minter");
            self.erc20_balances.erc20_sub(*tx.destination(), value);
        }
```

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/responses.rs (L9-35)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Decode, Deserialize, Encode, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TransactionReceipt {
    /// The hash of the block containing the transaction.
    #[n(0)]
    pub block_hash: Hash,

    /// The number of the block containing the transaction.
    #[n(1)]
    pub block_number: BlockNumber,

    /// The total base charge plus tip paid for each unit of gas
    #[n(2)]
    pub effective_gas_price: WeiPerGas,

    /// The amount of gas used by this specific transaction alone
    #[n(3)]
    pub gas_used: GasAmount,

    /// Status of the transaction.
    #[n(4)]
    pub status: TransactionStatus,

    /// The hash of the transaction
    #[n(5)]
    pub transaction_hash: Hash,
}
```

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/responses.rs (L45-62)
```rust
impl From<EvmTransactionReceipt> for TransactionReceipt {
    fn from(transaction_receipt: EvmTransactionReceipt) -> Self {
        Self {
            block_hash: Hash(transaction_receipt.block_hash.into()),
            block_number: BlockNumber::from(transaction_receipt.block_number),
            effective_gas_price: WeiPerGas::from(transaction_receipt.effective_gas_price),
            gas_used: GasAmount::from(transaction_receipt.gas_used),
            status: TransactionStatus::try_from(
                transaction_receipt
                    .status
                    .and_then(|s| s.as_ref().0.to_u8())
                    .expect("EvmTransactionReceipt.status should be Some(0) or Some(1)"),
            )
            .expect("EvmTransactionReceipt.status should be Some(0) or Some(1)"),
            transaction_hash: Hash(transaction_receipt.transaction_hash.into()),
        }
    }
}
```
