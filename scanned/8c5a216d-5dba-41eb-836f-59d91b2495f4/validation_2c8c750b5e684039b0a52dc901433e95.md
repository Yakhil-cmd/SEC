### Title
ETH Sent Directly to ckETH Minter's Ethereum Address Is Permanently Locked - (File: rs/ethereum/cketh/minter/cketh_minter.did)

### Summary
The ckETH minter controls an Ethereum address via threshold ECDSA. ETH sent directly to this address — bypassing the helper smart contract — is permanently inaccessible to the minting protocol. The minter only detects deposits by scraping `ReceivedEth` events emitted by the helper contract; direct ETH transfers produce no such event. There is no on-chain recovery path: the minter's withdrawal flow requires burning ckETH first, but no ckETH is ever minted for a direct deposit, so the ETH is stranded. Recovery requires an elaborate out-of-band workaround (an NNS upgrade proposal to add a sweep mechanism), mirroring the Compound Timelock pattern exactly.

### Finding Description
The minter exposes its Ethereum address via the `minter_address` endpoint. The DID file itself carries the warning:

> "IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter knows to which IC principal the funds should be deposited." [1](#0-0) 

The deposit pipeline works as follows: the minter runs a timer that calls `eth_getLogs` on the helper smart contract address and parses `ReceivedEth` / `ReceivedEthOrErc20` events. Only events emitted by the helper contract trigger minting. [2](#0-1) 

A direct ETH transfer to the minter's Ethereum address emits no helper-contract event. The minter's scraping loop never sees it, so no `AcceptedEthDepositRequest` event is recorded and no ckETH is minted.

The withdrawal path (`withdraw_eth`) burns ckETH from the caller's ledger account and queues an Ethereum transaction. Because no ckETH was minted for the direct deposit, there is nothing to burn, and the ETH cannot be withdrawn through the normal flow. [3](#0-2) 

The minter has no "sweep" or "claim" endpoint. The only recovery path is an NNS governance proposal to upgrade the minter with a new function that issues an arbitrary Ethereum transaction from the minter's address — an elaborate workaround identical in structure to the Compound Timelock issue described in the report.

### Impact Explanation
Any ETH sent directly to the minter's Ethereum address is stranded. The minter's balance on Ethereum grows, but the corresponding ckETH is never minted. The funds cannot be recovered without an NNS upgrade proposal that adds a bespoke sweep mechanism. This is a ledger conservation / chain-fusion funds-locking bug: real ETH value is absorbed by the minter canister with no protocol-level path to retrieve it.

### Likelihood Explanation
Medium. The minter's Ethereum address is publicly queryable via `minter_address`. Ethereum users accustomed to sending ETH directly to a contract address (e.g., a multisig or a vault) may do so here. The warning in the DID file is not enforced at the protocol level and is invisible to users interacting via raw Ethereum tooling (MetaMask, Etherscan, hardware wallets). The scenario has already occurred with analogous bridge contracts on other chains.

### Recommendation
Add a protocol-level mechanism to account for ETH that arrives at the minter's address outside the helper-contract flow. Two complementary approaches:

1. **Balance reconciliation**: On each scraping cycle, compare the minter's actual Ethereum balance against the expected balance derived from processed deposits minus processed withdrawals. Any surplus indicates a direct deposit; record it as a quarantined event and expose a governance-gated endpoint to assign it to a principal or return it.

2. **Sweep endpoint**: Add an NNS-only endpoint (callable only by the NNS root or governance canister) that issues an Ethereum transaction from the minter's address to a designated recovery address, analogous to the OpenZeppelin `TimelockController` approach referenced in the original report.

### Proof of Concept

1. Call `minter_address` on the ckETH minter canister (`sv3dd-oaaaa-aaaar-qacoa-cai`) to obtain the minter's Ethereum address.
2. Send ETH directly to that address from any Ethereum wallet (not via the helper contract at `smart_contract_address`).
3. Observe that no `ReceivedEth` event is emitted by the helper contract.
4. Observe that the minter's scraping loop (`eth_getLogs` against the helper contract) never records the deposit.
5. Observe that no ckETH is minted to any IC principal.
6. Attempt `withdraw_eth` — it fails with `InsufficientFunds` because no ckETH balance exists.
7. The ETH remains at the minter's Ethereum address indefinitely with no protocol-level recovery path. [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/ethereum/cketh/docs/cketh.adoc (L151-180)
```text
----

[TIP]
.Conversion ETH <--> Wei
====
The amounts described below use the smallest denomination of ETH called **wei**, where
`1 ETH = 1_000_000_000_000_000_000 WEI` (Ethereum uses 18 decimals).
You can use link:https://eth-converter.com/[this converter] to convert ETH to wei.
====

The first time a user wants to withdraw some ckETH, two steps are needed:

1. Approve the minter's principal on the ledger for the desired amount.
+
[source,shell]
----
dfx canister --network ic call ledger icrc2_approve "(record { spender = record { owner = principal \"$(dfx canister id minter --network ic)\" }; amount = LARGE_AMOUNT_WEI })"
----
2. Call the minter to make a withdrawal for the desired amount.
+
[source,shell]
----
dfx canister --network ic call minter withdraw_eth "(record {amount = SMALL_AMOUNT_WEI; recipient = \"YOUR_ETH_ADDRESS\"})"
----

Additional withdrawals could be made as long as the allowance from step 1 was not exhausted or did not time out.

After calling `withdraw_eth`, the minter will usually send a transaction to the Ethereum network within 6 minutes. Additional delays may occasionally occur due to reasons such as congestion on the Ethereum network or some Ethereum JSON-RPC providers being offline.

=== Example of a withdrawal
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L181-191)
```text

[WARNING]
.Supported ERC-20 tokens
====
Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it. To avoid any loss of funds, please verify **before** any important transfer that the desired ERC-20 token is supported by querying the minter as follows
and checking the field `supported_ckerc20_tokens`:
[source,shell]
----
dfx canister --network ic call minter get_minter_info
----
====
```
