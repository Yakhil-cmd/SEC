### Title
ERC20 Fee-on-Transfer Tokens Cause ckERC20 Over-Minting via Unverified `amount` in `depositErc20` Event - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

---

### Summary

The `depositErc20` function in the ckETH helper smart contract emits the caller-supplied `amount` in the `ReceivedEthOrErc20` event without verifying the actual amount received by the minter address after the `safeTransferFrom` call. For fee-on-transfer ERC20 tokens, the actual amount credited to the minter is less than `amount`, but the ckETH minter on the IC reads the emitted `amount` and mints that many ckERC20 tokens, creating unbacked supply and breaking the 1:1 backing guarantee.

---

### Finding Description

In `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, the `depositErc20` function performs the following sequence:

```solidity
erc20Token.safeTransferFrom(
    msg.sender,
    minterAddress,
    amount          // requested amount
);

emit ReceivedEthOrErc20(
    erc20Address,
    msg.sender,
    amount,         // ← emits requested amount, NOT actual received amount
    principal,
    subaccount
);
``` [1](#0-0) 

For a fee-on-transfer ERC20 token, `safeTransferFrom` succeeds but `minterAddress` receives `amount - fee`, not `amount`. The event still records `amount` (the requested value), not the actual balance increase.

The ckETH minter on the IC scrapes these Ethereum logs and mints ckERC20 tokens using `event.value()`, which is derived directly from the `uint256 amount` field of the `ReceivedEthOrErc20` event:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),   // ← sourced from the emitted `amount`, not actual received
    })
    .await
``` [2](#0-1) 

The minter records the mint as successful and marks the event as processed: [3](#0-2) 

The `record_successful_mint` path in the minter state permanently records the full `amount` as minted: [4](#0-3) 

---

### Impact Explanation

**Vulnerability class**: chain-fusion mint/burn/replay bug / ledger conservation bug.

For any ckERC20 token whose underlying ERC20 contract implements a transfer fee (fee-on-transfer), every `depositErc20` call mints more ckERC20 than the actual ERC20 held by the minter address. Over time, the ckERC20 total supply exceeds the ERC20 collateral, breaking the 1:1 backing guarantee. Users who withdraw last cannot redeem their ckERC20 for ERC20 because the minter's ERC20 balance is insufficient. This is a direct ledger conservation violation in the chain-fusion subsystem.

---

### Likelihood Explanation

**Medium.** Exploiting this requires a fee-on-transfer ERC20 token to be registered as a supported ckERC20 token via an NNS governance proposal. While current supported tokens (e.g., USDC, LINK) are not fee-on-transfer, the code is structurally vulnerable and any future addition of such a token — or a token that later introduces a fee mechanism via upgrade — would trigger the bug. Any Ethereum user can call `depositErc20` without privilege; no attacker capability beyond holding the token is required once the token is listed.

---

### Recommendation

Record the minter's ERC20 balance before and after the `safeTransferFrom` call, and emit the actual balance increase rather than the caller-supplied `amount`:

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
    require(actualReceived > 0, "ERC20: zero actual transfer");

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        actualReceived,   // ← actual received, not requested amount
        principal,
        subaccount
    );
}
```

This mirrors the mitigation recommended in the referenced external report and ensures the ckETH minter only mints ckERC20 tokens equal to the actual ERC20 collateral received.

---

### Proof of Concept

1. A fee-on-transfer ERC20 token (e.g., 1% fee on every transfer) is added as a supported ckERC20 token via NNS governance.
2. An unprivileged Ethereum user calls `depositErc20(tokenAddress, 10_000, icPrincipal, subaccount)`.
3. `safeTransferFrom` executes: the minter address receives `9_900` tokens (1% fee deducted), but the call succeeds.
4. The event emits `ReceivedEthOrErc20(tokenAddress, user, 10_000, icPrincipal, subaccount)` — recording `10_000`, not `9_900`.
5. The ckETH minter scrapes the log, reads `value = 10_000`, and calls `icrc1_transfer` to mint `10_000` ckERC20 to the user.
6. The user holds `10_000` ckERC20 but the minter holds only `9_900` ERC20 tokens.
7. `100` ckERC20 tokens are permanently unbacked. Repeated deposits compound the deficit until the minter cannot honor all withdrawals. [1](#0-0) [5](#0-4)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-102)
```rust
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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L103-128)
```rust
        mutate_state(|s| {
            process_event(
                s,
                match &event {
                    ReceivedEvent::Eth(event) => EventType::MintedCkEth {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                    },

                    ReceivedEvent::Erc20(event) => EventType::MintedCkErc20 {
                        event_source: event.source(),
                        mint_block_index: LedgerMintIndex::new(block_index),
                        erc20_contract_address: event.erc20_contract_address,
                        ckerc20_token_symbol: token_symbol.clone(),
                    },
                },
            )
        });
        log!(
            INFO,
            "Minted {} {token_symbol} to {} in block {block_index}",
            event.value(),
            event.beneficiary()
        );
        // minting succeeded, defuse guard
        ScopeGuard::into_inner(prevent_double_minting_guard);
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L274-302)
```rust
    fn record_successful_mint(
        &mut self,
        source: EventSource,
        token_symbol: &str,
        mint_block_index: LedgerMintIndex,
        erc20_contract_address: Option<Address>,
    ) {
        assert!(
            !self.invalid_events.contains_key(&source),
            "attempted to mint an event previously marked as invalid {source:?}"
        );
        let deposit_event = match self.events_to_mint.remove(&source) {
            Some(event) => event,
            None => panic!("attempted to mint ckETH for an unknown event {source:?}"),
        };
        assert_eq!(
            self.minted_events.insert(
                source,
                MintedEvent {
                    deposit_event,
                    mint_block_index,
                    token_symbol: token_symbol.to_string(),
                    erc20_contract_address,
                },
            ),
            None,
            "attempted to mint ckETH twice for the same event {source:?}"
        );
    }
```
