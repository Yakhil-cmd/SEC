### Title
ckETH Minter Critical Canister IDs (`evm_rpc_id`, `ledger_suite_orchestrator_id`) Can Be Changed to Arbitrary Principals Without Existence or Interface Validation — (File: `rs/ethereum/cketh/minter/src/state.rs`)

---

### Summary

The ckETH minter's `upgrade()` function accepts `evm_rpc_id` and `ledger_suite_orchestrator_id` as mutable upgrade arguments and applies them directly to state without any validation that the supplied principal is a live canister or implements the expected interface. This is the direct IC analog of the "yieldTrackers can be changed anytime" finding: a governance-controlled address change with no defensive checks, capable of silently bricking the entire ckETH/ckERC20 deposit and withdrawal pipeline.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/state.rs`, the private `upgrade()` method processes the `UpgradeArg` struct passed during `post_upgrade`. Two fields are applied unconditionally with no canister-existence or interface-compliance check:

```rust
// rs/ethereum/cketh/minter/src/state.rs lines 531-536
if let Some(orchestrator_id) = ledger_suite_orchestrator_id {
    self.ledger_suite_orchestrator_id = Some(orchestrator_id);
}
if let Some(evm_id) = evm_rpc_id {
    self.evm_rpc_id = evm_id;
}
``` [1](#0-0) 

The only post-assignment check is `self.validate_config()`, which verifies internal state consistency (e.g., that required fields are non-empty) but does **not** verify that the new principal is a deployed canister, that it responds to the EVM RPC or orchestrator interface, or that it is on the correct subnet. [2](#0-1) 

Both fields are exposed in the public `UpgradeArg` Candid type:

```
// rs/ethereum/cketh/minter/cketh_minter.did lines 129, 139
ledger_suite_orchestrator_id : opt principal;
evm_rpc_id : opt principal;
``` [3](#0-2) 

The `evm_rpc_id` is the sole canister through which the ckETH minter communicates with the Ethereum blockchain (log scraping, transaction submission). The `ledger_suite_orchestrator_id` is the canister notified when new ckERC20 tokens are added. Both are critical to liveness.

---

### Impact Explanation

**`evm_rpc_id` changed to a non-existent or non-conforming principal:**
- All Ethereum log scraping stops → no new ckETH or ckERC20 deposits are ever minted.
- All ETH/ERC-20 withdrawal transactions fail to be submitted → pending withdrawals are permanently stuck.
- The minter enters a silent DoS: it continues to accept `withdraw_eth` and `retrieve_eth` calls, burns ckETH from users' balances, but never produces an on-chain Ethereum transaction.

**`ledger_suite_orchestrator_id` changed to an invalid principal:**
- The minter's `add_ckerc20_token` endpoint (called by the orchestrator) will reject calls from the legitimate orchestrator, preventing new ckERC20 tokens from being activated.
- Existing ckERC20 tokens are unaffected, but the system cannot be extended.

Both impacts are persistent until a corrective NNS upgrade proposal is passed and executed.

---

### Likelihood Explanation

The change requires an NNS governance proposal to upgrade the ckETH minter with a crafted `UpgradeArg`. This is the same governance pathway used for all legitimate minter upgrades (e.g., the mainnet upgrade proposals in `rs/ethereum/cketh/mainnet/`). The risk is not limited to malicious intent: an accidental typo in a proposal's `evm_rpc_id` field, a copy-paste error from a staging canister ID, or a proposal that targets the wrong network's RPC canister would silently corrupt the minter's routing with no on-upgrade rejection. The absence of a liveness check means the error is not caught at upgrade time but only discovered when the minter's timer tasks begin failing. [4](#0-3) 

---

### Recommendation

1. **Canister existence check at upgrade time**: Before committing a new `evm_rpc_id` or `ledger_suite_orchestrator_id`, call `canister_status` on the management canister for the supplied principal. Reject the upgrade (via `ic_cdk::trap`) if the call fails.

2. **Interface probe**: After confirming existence, issue a lightweight query call (e.g., `request_cost` on the EVM RPC canister) to verify the canister responds to the expected interface before committing the new ID.

3. **Immutability guard for `evm_rpc_id`**: Consider making `evm_rpc_id` immutable after initialization (similar to `cketh_ledger_id` and `ecdsa_key_name`, which are not present in `UpgradeArg`), requiring a full reinstall to change it. This matches the treatment of other security-critical identifiers in the same state struct. [5](#0-4) 

---

### Proof of Concept

1. Construct a `MinterArg::UpgradeArg` with `evm_rpc_id = opt principal "aaaaa-aa"` (the management canister, which does not implement the EVM RPC interface).
2. Submit an NNS proposal: `propose-to-change-nns-canister --canister-id sv3dd-oaaaa-aaaar-qacoa-cai --mode upgrade --arg <encoded_arg>`.
3. Upon proposal execution, `post_upgrade` calls `upgrade()`, which sets `self.evm_rpc_id = Principal::management_canister()` and passes `validate_config()` without error.
4. On the next timer tick, the minter attempts to call `request_cost` or `eth_getLogs` on `aaaaa-aa`; the call is rejected with a canister method not found error.
5. The minter's `active_tasks` guard prevents re-entry, but the task is rescheduled and fails on every subsequent timer tick.
6. All ckETH deposits and withdrawals are permanently halted until a corrective upgrade proposal is passed. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/state.rs (L450-538)
```rust
    fn upgrade(&mut self, upgrade_args: UpgradeArg) -> Result<(), InvalidStateError> {
        use std::str::FromStr;

        let UpgradeArg {
            next_transaction_nonce,
            minimum_withdrawal_amount,
            ethereum_contract_address,
            ethereum_block_height,
            ledger_suite_orchestrator_id,
            erc20_helper_contract_address,
            last_erc20_scraped_block_number,
            evm_rpc_id,
            deposit_with_subaccount_helper_contract_address,
            last_deposit_with_subaccount_scraped_block_number,
        } = upgrade_args;
        if let Some(nonce) = next_transaction_nonce {
            let nonce = TransactionNonce::try_from(nonce)
                .map_err(|e| InvalidStateError::InvalidTransactionNonce(format!("ERROR: {e}")))?;
            self.eth_transactions.update_next_transaction_nonce(nonce);
        }
        if let Some(amount) = minimum_withdrawal_amount {
            let minimum_withdrawal_amount = Wei::try_from(amount).map_err(|e| {
                InvalidStateError::InvalidMinimumWithdrawalAmount(format!("ERROR: {e}"))
            })?;
            self.cketh_minimum_withdrawal_amount = minimum_withdrawal_amount;
        }
        if let Some(address) = ethereum_contract_address {
            let eth_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::EthDepositWithoutSubaccount,
                    eth_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(address) = erc20_helper_contract_address {
            let erc20_helper_contract_address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(
                    LogScrapingId::Erc20DepositWithoutSubaccount,
                    erc20_helper_contract_address,
                )
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(block_number) = last_erc20_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::Erc20DepositWithoutSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(address) = deposit_with_subaccount_helper_contract_address {
            let address = Address::from_str(&address).map_err(|e| {
                InvalidStateError::InvalidErc20HelperContractAddress(format!("ERROR: {e}"))
            })?;
            self.log_scrapings
                .set_contract_address(LogScrapingId::EthOrErc20DepositWithSubaccount, address)
                .map_err(|e| {
                    InvalidStateError::InvalidEthereumContractAddress(format!("ERROR: {e:?}"))
                })?;
        }
        if let Some(block_number) = last_deposit_with_subaccount_scraped_block_number {
            self.log_scrapings.set_last_scraped_block_number(
                LogScrapingId::EthOrErc20DepositWithSubaccount,
                BlockNumber::try_from(block_number).map_err(|e| {
                    InvalidStateError::InvalidLastErc20ScrapedBlockNumber(format!("ERROR: {e}"))
                })?,
            );
        }
        if let Some(block_height) = ethereum_block_height {
            self.ethereum_block_height = block_height;
        }
        if let Some(orchestrator_id) = ledger_suite_orchestrator_id {
            self.ledger_suite_orchestrator_id = Some(orchestrator_id);
        }
        if let Some(evm_id) = evm_rpc_id {
            self.evm_rpc_id = evm_id;
        }
        self.validate_config()
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L114-146)
```text
type UpgradeArg = record {
    // Change the nonce of the next transaction to be sent to the Ethereum network.
    next_transaction_nonce : opt nat;

    // Change the minimum amount in Wei that can be withdrawn.
    minimum_withdrawal_amount : opt nat;

    // Change the ETH helper smart contract address.
    ethereum_contract_address : opt text;

    // Change the ethereum block height observed by the minter.
    ethereum_block_height : opt BlockTag;

    // The principal of the ledger suite orchestrator that handles the ICRC1 ledger suites
    // for all ckERC20 tokens.
    ledger_suite_orchestrator_id : opt principal;

    // Change the ERC-20 helper smart contract address.
    erc20_helper_contract_address : opt text;

    // Change the last scraped block number of the ERC-20 helper smart contract.
    last_erc20_scraped_block_number : opt nat;

    // The principal of the EVM RPC canister that handles the communication
    // with the Ethereum blockchain.
    evm_rpc_id : opt principal;

    // Change the deposit with subaccount helper smart contract address.
    deposit_with_subaccount_helper_contract_address : opt text;

    // Change the last scraped block number of the deposit with subaccount helper smart contract.
    last_deposit_with_subaccount_scraped_block_number : opt nat;
};
```
