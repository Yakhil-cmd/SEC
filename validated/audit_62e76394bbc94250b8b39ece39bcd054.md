### Title
Fee-on-Transfer ERC20 Deposit Mints Unbacked ckERC20 Tokens - (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The `depositErc20` function in the ckETH helper smart contract emits the `ReceivedEthOrErc20` event using the caller-supplied `amount` parameter rather than the actual tokens received by the minter address. The IC ckETH minter reads this event and mints exactly `event.value()` ckERC20 tokens to the beneficiary. For any fee-on-transfer ERC20 token, the minter receives fewer tokens than it mints, permanently breaking the 1:1 backing invariant.

---

### Finding Description

In `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, the `depositErc20` function calls `safeTransferFrom` and then emits the event with the original `amount` argument:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        amount,        // ← requested amount, NOT actual received amount
        principal,
        subaccount
    );
}
``` [1](#0-0) 

For a fee-on-transfer ERC20 token (e.g., one that deducts 5% on every transfer), `safeTransferFrom(user, minter, 1000)` results in the minter receiving only 950 tokens, but the event records `amount = 1000`.

The IC ckETH minter scrapes these logs and mints ckERC20 using `event.value()` directly:

```rust
let block_index = match client
    .transfer(TransferArg {
        to: event.beneficiary(),
        amount: event.value(),   // ← taken verbatim from the Ethereum event log
        ...
    })
``` [2](#0-1) 

There is no balance-delta check between the minter's ERC20 balance before and after the `safeTransferFrom`. The minter unconditionally trusts the event's `amount` field.

---

### Impact Explanation

**Ledger conservation / chain-fusion mint bug.** Each deposit of a fee-on-transfer ERC20 token creates a surplus of ckERC20 tokens relative to the actual ERC20 tokens held by the minter. Over time, the total ckERC20 supply exceeds the minter's ERC20 balance. When users attempt to withdraw, the minter cannot fulfill all redemptions — the last withdrawers receive nothing. The 1:1 peg invariant that underpins the entire ckERC20 system is permanently broken for that token.

---

### Likelihood Explanation

The minter only processes logs for **supported** ERC20 tokens — those added via NNS governance proposal. This limits the attack surface: a fee-on-transfer token must be whitelisted. However:

1. Fee-on-transfer tokens are a well-known ERC20 pattern (e.g., PAXG, STA, USDT on some chains with fee enabled). A governance proposal to add such a token could pass without reviewers noticing the fee-on-transfer behavior.
2. The vulnerability requires no privileged IC access beyond the normal governance process — any NNS participant can submit a proposal.
3. The minter documentation explicitly warns about unsupported tokens but does not warn about fee-on-transfer tokens among supported ones. [3](#0-2) 

---

### Recommendation

Measure the actual balance delta in the Solidity helper contract and emit that value in the event:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
}
```

Additionally, the NNS governance process for adding new ckERC20 tokens should explicitly screen for fee-on-transfer behavior.

---

### Proof of Concept

1. A fee-on-transfer ERC20 token `FeeToken` (5% fee per transfer) is added as a supported ckERC20 token via NNS proposal.
2. Attacker calls `depositErc20(FeeToken, 1_000_000, principal, subaccount)` on the helper contract.
3. `safeTransferFrom(attacker, minter, 1_000_000)` executes; minter receives `950_000` FeeToken (5% fee deducted).
4. The helper emits `ReceivedEthOrErc20(FeeToken, attacker, 1_000_000, principal, subaccount)`.
5. The IC minter scrapes the log, reads `value = 1_000_000`, and calls `icrc1_transfer` to mint `1_000_000` ckFeeToken to the attacker. [4](#0-3) 

6. Attacker now holds `1_000_000` ckFeeToken; minter holds only `950_000` FeeToken.
7. Repeating this inflates the unbacked surplus. When the minter's FeeToken balance is exhausted, legitimate ckFeeToken holders cannot redeem.

The root cause is confirmed at:
- Solidity: `amount` emitted without balance-delta check. [5](#0-4) 
- Rust minter: `event.value()` minted without verification. [6](#0-5)

### Citations

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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L182-191)
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
