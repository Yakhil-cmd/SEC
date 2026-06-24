### Title
ckERC20 Deposit Helper Contracts Emit Requested Amount Instead of Actual Received Amount, Enabling Over-Minting for Fee-on-Transfer Tokens - (File: rs/ethereum/cketh/minter/ERC20DepositHelper.sol, rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

---

### Summary

Both ckERC20 helper smart contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) emit the caller-supplied `amount` parameter in the deposit event rather than the actual balance difference received by the minter address. The IC ckETH minter canister scrapes these events and mints ckERC20 tokens equal to the event-reported `amount` with no on-chain balance verification. For any ERC-20 token that charges a transfer fee (fee-on-transfer), the minter's Ethereum address receives fewer tokens than the event records, but the IC minter mints the full event amount, creating unbacked ckERC20 supply.

---

### Finding Description

**`ERC20DepositHelper.sol` — `CkErc20Deposit.deposit`:**

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

**`DepositHelperWithSubaccount.sol` — `CkDeposit.depositErc20`:**

```solidity
function depositErc20(address erc20Address, uint256 amount, ...) public {
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [2](#0-1) 

In both cases, the emitted `amount` is the caller-supplied parameter, not `balanceOf(minterAddress)_after - balanceOf(minterAddress)_before`. For a fee-on-transfer ERC-20, the actual credit to the minter is `amount - fee`, but the event records `amount`.

The IC minter's log parser reads the event data field directly as the mint value:

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
// ...
value: Erc20Value::from_be_bytes(value_bytes),
``` [3](#0-2) 

The `mint()` function in `deposit.rs` then mints exactly `event.value()` ckERC20 tokens to the beneficiary with no further balance reconciliation:

```rust
.transfer(TransferArg {
    amount: event.value(),
    ...
})
``` [4](#0-3) 

There is no step anywhere in `scrape_logs`, `register_deposit_events`, or `mint` that queries the minter's actual ERC-20 balance before and after to compute the real received amount. [5](#0-4) 

---

### Impact Explanation

**Vulnerability class: chain-fusion mint/burn accounting bug.**

If a supported ckERC20 token implements a transfer fee (either at launch or via a subsequent upgrade of the ERC-20 contract), every deposit call will cause the IC minter to mint more ckERC20 than the ERC-20 tokens it actually holds. Over time, the total ckERC20 supply exceeds the minter's ERC-20 reserve. When users attempt to withdraw ckERC20 back to ERC-20, the minter will be unable to fulfill all withdrawal requests — the last withdrawers receive nothing. This is a direct ledger conservation break for the ckERC20 token.

**Impact: High** — unbacked ckERC20 tokens are minted; the peg between ckERC20 and the underlying ERC-20 is broken, causing loss of funds for users who cannot withdraw.

---

### Likelihood Explanation

The minter enforces a whitelist of supported ERC-20 tokens via NNS governance proposals. However:

1. A token's contract can be upgraded after it is whitelisted (proxy-pattern ERC-20s such as USDC/USDT are upgradeable).
2. A future NNS proposal could add a fee-on-transfer token without the community recognizing the protocol-level risk.
3. No attacker privilege is required beyond submitting a normal deposit transaction to the helper contract — the exploit path is fully unprivileged once a fee-on-transfer token is supported.

**Likelihood: Low-Medium** — requires a whitelisted token to have fee-on-transfer behavior, but the protocol provides no defense-in-depth against this scenario.

---

### Recommendation

Both helper contracts should measure the actual balance delta and emit that as the deposit amount:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "...");
    IERC20 erc20Token = IERC20(erc20Address);
    uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    uint256 actualAmount = erc20Token.balanceOf(minterAddress) - balanceBefore;
    emit ReceivedEthOrErc20(erc20Address, msg.sender, actualAmount, principal, subaccount);
}
```

This mirrors the fix described in the reference report and ensures the IC minter only mints ckERC20 equal to the tokens it actually received.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token (e.g., 1% fee on every `transferFrom`) is added as a supported ckERC20 token via NNS proposal.
2. Attacker calls `depositErc20(tokenAddr, 1_000_000, principal, subaccount)` on `CkDeposit`.
3. The helper calls `safeTransferFrom(attacker, minterAddress, 1_000_000)`. Due to the 1% fee, minter receives `990_000` tokens.
4. The helper emits `ReceivedEthOrErc20(..., 1_000_000, ...)`.
5. The IC minter scrapes the log, parses `value = 1_000_000` from the event data, and mints `1_000_000` ckERC20 to the attacker's IC account.
6. Attacker now holds `1_000_000` ckERC20 backed by only `990_000` ERC-20 tokens.
7. Repeated deposits drain the reserve. The final `10_000` ckERC20 tokens per deposit cycle are permanently unbacked, breaking the 1:1 peg and causing loss for other ckERC20 holders attempting to withdraw.

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-97)
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L73-81)
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L311-345)
```rust
pub fn register_deposit_events(
    scraping_id: LogScrapingId,
    transaction_events: Vec<ReceivedEvent>,
    errors: Vec<ReceivedEventError>,
) {
    for event in transaction_events {
        log!(
            INFO,
            "Received event {event:?}; will mint {} {scraping_id} to {}",
            event.value(),
            event.beneficiary()
        );
        if crate::blocklist::is_blocked(&event.from_address()) {
            log!(
                INFO,
                "Received event from a blocked address: {} for {} {scraping_id}",
                event.from_address(),
                event.value(),
            );
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
    }
    if read_state(State::has_events_to_mint) {
        ic_cdk_timers::set_timer(Duration::from_secs(0), async { mint().await });
    }
```
