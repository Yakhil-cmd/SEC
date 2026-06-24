Audit Report

## Title
ckERC20 Minter Over-Mints Tokens for Fee-on-Transfer ERC-20 Deposits Due to Trusting Event-Emitted Amount - (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

## Summary
Both Ethereum helper contracts (`ERC20DepositHelper.sol` and `DepositHelperWithSubaccount.sol`) emit deposit events containing the caller-supplied `amount` parameter rather than the actual tokens received by the minter address. The IC minter scrapes these logs and mints exactly `event.value()` of ckERC20 without any on-chain balance verification. For fee-on-transfer ERC-20 tokens, the minter receives `amount - fee` but mints `amount` of ckERC20, permanently undercollateralizing the ckERC20 supply.

## Finding Description

**Solidity layer — event emits requested amount, not received amount:**

`ERC20DepositHelper.sol` `deposit` function calls `safeTransferFrom` then emits the caller-supplied `amount`: [1](#0-0) 

`DepositHelperWithSubaccount.sol` `depositErc20` does the same: [2](#0-1) 

Neither contract records the minter's balance before/after `safeTransferFrom`. For a fee-on-transfer token, `safeTransferFrom(user, minter, amount)` delivers `amount - fee` to the minter, but the emitted event still carries `amount`.

**IC minter layer — blindly trusts the log-emitted value:**

The `ReceivedErc20Event` struct stores the `value` field directly from the parsed log: [3](#0-2) 

`ReceivedErc20LogParser` and `ReceivedEthOrErc20LogParser` both decode `value_bytes` from the event data and store it verbatim: [4](#0-3) 

The `mint()` function in `deposit.rs` then calls `client.transfer(TransferArg { amount: event.value(), ... })` with no balance-before/after check: [5](#0-4) 

**No existing guard addresses this:** The only checks in `mint()` are double-minting prevention (scopeguard) and blocklist filtering. There is no verification that the minter's actual on-chain ERC-20 balance increased by `event.value()`.

**Token addition path has no fee-on-transfer restriction:** `add_ckerc20_token` only validates network, symbol format, and uniqueness — no check for fee-on-transfer behavior: [6](#0-5) 

## Impact Explanation

Each deposit of a fee-on-transfer ERC-20 token inflates the ckERC20 supply beyond the actual ERC-20 collateral held by the minter. The gap compounds with every deposit. When users later withdraw ckERC20 back to ERC-20, the minter's Ethereum balance is insufficient to cover all outstanding ckERC20 — the last withdrawers receive less ERC-20 than their ckERC20 represents, or withdrawals fail entirely. This constitutes **illegal minting and protocol insolvency for an in-scope ck-token**, matching the **High** impact tier: "Significant Chain Fusion, ck-token, ledger security impact with concrete user or protocol harm." USDT — already a supported ckERC20 token — contains a dormant fee mechanism in its contract that the USDT issuer can enable at any time without IC governance involvement, making the precondition realistic rather than hypothetical. [7](#0-6) 

## Likelihood Explanation

The minter currently supports USDT, which has a fee mechanism set to zero but activatable by the USDT issuer unilaterally. Any future NNS proposal adding a fee-on-transfer token would also trigger this. Once a fee-on-transfer token is in the supported list, the exploit requires no special privileges: any user calling `depositErc20(feeToken, amount, principal, subaccount)` on the helper contract triggers the over-mint. The exploit is repeatable, permissionless, and compounds with volume. Likelihood is **Medium** — not currently exploitable against live tokens, but a realistic near-future risk given USDT's dormant fee and the open token-addition governance path.

## Recommendation

1. **In the Solidity helper contracts**: Record the minter's ERC-20 balance before and after `safeTransferFrom` and emit `balanceAfter - balanceBefore` as the deposit value instead of the caller-supplied `amount`. This is the authoritative fix.

2. **In the IC minter**: As defense-in-depth, when processing a `ReceivedErc20Event`, verify via an `eth_call` to `balanceOf(minterAddress)` that the minter's on-chain balance increased by at least `event.value()` before minting. If the balance delta is less, mint only the actual received amount.

3. **In the token-addition governance process**: Explicitly document and enforce that only non-fee-on-transfer ERC-20 tokens may be added as supported ckERC20 tokens. Add a validation step in `validate_add_erc20` or the NNS proposal review process to check for fee-on-transfer behavior.

## Proof of Concept

1. A fee-on-transfer ERC-20 token (1% fee) is added as a supported ckERC20 token via NNS proposal, OR USDT enables its dormant fee mechanism.
2. User calls `depositErc20(feeToken, 1_000_000, principal, subaccount)` on `DepositHelperWithSubaccount`.
3. Helper calls `safeTransferFrom(user, minter, 1_000_000)` — minter receives `990_000` tokens due to 1% fee.
4. Helper emits `ReceivedEthOrErc20(feeToken, user, 1_000_000, principal, subaccount)`.
5. IC minter scrapes the log via `ReceivedEthOrErc20LogParser::parse_log`, stores `value = 1_000_000`.
6. `mint()` calls `client.transfer(TransferArg { amount: 1_000_000, ... })` — mints `1_000_000` ckFeeToken to user.
7. Minter holds `990_000` ERC-20 but has issued `1_000_000` ckERC20 — `10_000` tokens unbacked.
8. Repeated deposits compound the gap. A local fork/PocketIC integration test can reproduce this by deploying a mock fee-on-transfer ERC-20, registering it as a supported ckERC20 token, submitting a deposit transaction, and asserting that `ckERC20.totalSupply() > erc20.balanceOf(minterAddress)` after minting. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L511-532)
```text
    function depositErc20(
        address erc20Address,
        uint256 amount,
        bytes32 principal,
        bytes32 subaccount
    ) public {
        require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
        IERC20 erc20Token = IERC20(erc20Address);
        erc20Token.safeTransferFrom(
            msg.sender,
            minterAddress,
            amount
        );

        emit ReceivedEthOrErc20(
            erc20Address,
            msg.sender,
            amount,
            principal,
            subaccount
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L57-75)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Decode, Encode)]
pub struct ReceivedErc20Event {
    #[n(0)]
    pub transaction_hash: Hash,
    #[n(1)]
    pub block_number: BlockNumber,
    #[cbor(n(2))]
    pub log_index: LogIndex,
    #[n(3)]
    pub from_address: Address,
    #[n(4)]
    pub value: Erc20Value,
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub principal: Principal,
    #[n(6)]
    pub erc20_contract_address: Address,
    #[n(7)]
    pub subaccount: Option<LedgerSubaccount>,
}
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-103)
```rust
        let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value: Erc20Value::from_be_bytes(value_bytes),
            principal,
            erc20_contract_address,
            subaccount: None,
        }
        .into())
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L40-102)
```rust
    for event in events {
        // Ensure that even if we were to panic in the callback, after having contacted the ledger to mint the tokens,
        // this event will not be processed again.
        let prevent_double_minting_guard = scopeguard::guard(event.clone(), |event| {
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::QuarantinedDeposit {
                        event_source: event.source(),
                    },
                )
            });
        });
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
            }
        };
        let client = ICRC1Client {
            runtime: CdkRuntime,
            ledger_canister_id,
        };
        let block_index = match client
            .transfer(TransferArg {
                from_subaccount: None,
                to: event.beneficiary(),
                fee: None,
                created_at_time: None,
                memo: Some((&event).into()),
                amount: event.value(),
            })
            .await
        {
            Ok(Ok(block_index)) => block_index.0.to_u64().expect("nat does not fit into u64"),
            Ok(Err(err)) => {
                log!(INFO, "Failed to mint {token_symbol}: {event:?} {err}");
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
            Err(err) => {
                log!(
                    INFO,
                    "Failed to send a message to the ledger ({ledger_canister_id}): {err:?}"
                );
                error_count += 1;
                // minting failed, defuse guard
                ScopeGuard::into_inner(prevent_double_minting_guard);
                continue;
            }
        };
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L562-574)
```rust
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    let orchestrator_id = read_state(|s| s.ledger_suite_orchestrator_id)
        .unwrap_or_else(|| ic_cdk::trap("ERROR: ERC-20 feature is not activated"));
    if orchestrator_id != ic_cdk::api::msg_caller() {
        ic_cdk::trap(format!(
            "ERROR: only the orchestrator {orchestrator_id} can add ERC-20 tokens"
        ));
    }
    let ckerc20_token = erc20::CkErc20Token::try_from(erc20_token)
        .unwrap_or_else(|e| ic_cdk::trap(format!("ERROR: {e}")));
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L37-38)
```text
|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```
