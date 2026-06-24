### Title
Fee-on-Transfer ERC-20 Over-Minting: Helper Contracts Emit Input `amount` Instead of Actual Received Amount - (File: `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The ckERC20 deposit helper contracts (`CkErc20Deposit.deposit` and `CkDeposit.depositErc20`) emit the caller-supplied `amount` parameter in the `ReceivedErc20` / `ReceivedEthOrErc20` event **after** calling `safeTransferFrom`, without verifying the actual tokens received. For fee-on-transfer (FoT) ERC-20 tokens, the minter address receives `amount - fee`, but the event records `amount`. The IC minter canister reads the event value and mints exactly `event.value()` ckERC20 tokens, creating unbacked supply and breaking the 1:1 peg invariant.

---

### Finding Description

**Helper contract `CkErc20Deposit` (`ERC20DepositHelper.sol`):**

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal); // ← emits input param, not actual received
}
``` [1](#0-0) 

**Helper contract `CkDeposit` (`DepositHelperWithSubaccount.sol`):**

```solidity
function depositErc20(address erc20Address, uint256 amount, ...) public {
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount); // ← same issue
}
``` [2](#0-1) 

The IC minter canister scrapes these Ethereum logs and mints ckERC20 using `event.value()` directly:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),  // ← taken verbatim from the log event
    })
    .await
``` [3](#0-2) 

The `event.value()` method returns the `value` field parsed from the log data, which is the `amount` emitted by the helper contract — not the actual balance delta at the minter address:

```rust
pub fn value(&self) -> candid::Nat {
    match self {
        ReceivedEvent::Eth(evt) => evt.value.into(),
        ReceivedEvent::Erc20(evt) => evt.value.into(),
    }
}
``` [4](#0-3) 

The `value` field in `ReceivedErc20Event` is populated directly from the log's `data` field (the emitted `amount`), not from an on-chain balance check: [5](#0-4) 

---

### Impact Explanation

For any supported ckERC20 token that implements a transfer fee (fee-on-transfer pattern), the minter receives `amount - fee` ERC-20 tokens but mints `amount` ckERC20 tokens. Over time:

1. The minter's ERC-20 balance becomes insufficient to back all outstanding ckERC20 tokens.
2. The 1:1 peg invariant is broken — total ckERC20 supply exceeds actual ERC-20 held.
3. When users attempt to withdraw ckERC20 back to ERC-20, the minter cannot fulfill all requests, causing withdrawal failures for later users (last-out-loses scenario).
4. The minter's internal `erc20_balances` accounting (updated via `erc20_add(event.value)`) also becomes inflated relative to reality. [6](#0-5) 

---

### Likelihood Explanation

- Any unprivileged Ethereum user can call `deposit()` / `depositErc20()` on the helper contracts with any ERC-20 address — the contracts impose no token whitelist.
- The minter only processes events for NNS-governance-approved tokens, but if any approved token has FoT behavior (e.g., tokens with configurable fees, rebasing tokens, or tokens that add fees in future upgrades), the over-minting occurs automatically on every deposit.
- The helper contracts are immutable on-chain; the minter cannot retroactively correct already-emitted events.
- The vulnerability is triggered by normal user deposit behavior — no special privilege is required.

---

### Recommendation

In both helper contracts, measure the minter's ERC-20 balance before and after the `safeTransferFrom` call, and emit the **actual received amount** (the balance delta) in the event:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    uint256 balanceBefore = erc20Token.balanceOf(cketh_minter_main_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    uint256 actualReceived = erc20Token.balanceOf(cketh_minter_main_address) - balanceBefore;
    emit ReceivedErc20(erc20_address, msg.sender, actualReceived, principal);
}
```

This is the "FoT approach" recommended in the referenced Lido/Sophon report. Apply the same fix to `CkDeposit.depositErc20` in `DepositHelperWithSubaccount.sol`.

---

### Proof of Concept

1. A supported ckERC20 token (e.g., a token with a 1% transfer fee) is registered via NNS governance.
2. A user calls `deposit(tokenAddress, 1000, principalBytes)` on `CkErc20Deposit`.
3. `safeTransferFrom` transfers 990 tokens to the minter (10 taken as fee by the token contract).
4. The contract emits `ReceivedErc20(tokenAddress, user, 1000, principal)` — recording 1000, not 990.
5. The IC minter scrapes the log, reads `value = 1000`, and calls `icrc1_transfer` to mint 1000 ckERC20 to the user.
6. The minter holds 990 ERC-20 tokens but has minted 1000 ckERC20 — 10 tokens of unbacked supply created per deposit.
7. Repeated deposits accumulate the deficit until the minter cannot honor all withdrawal requests. [1](#0-0) [7](#0-6) [3](#0-2)

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L205-210)
```rust
    pub fn value(&self) -> candid::Nat {
        match self {
            ReceivedEvent::Eth(evt) => evt.value.into(),
            ReceivedEvent::Erc20(evt) => evt.value.into(),
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L86-102)
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L332-338)
```rust
    fn update_balance_upon_deposit(&mut self, event: &ReceivedEvent) {
        match event {
            ReceivedEvent::Eth(event) => self.eth_balance.eth_balance_add(event.value),
            ReceivedEvent::Erc20(event) => self
                .erc20_balances
                .erc20_add(event.erc20_contract_address, event.value),
        };
```
