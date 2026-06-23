### Title
ckERC20 Minter Mints Based on Event-Logged Amount, Not Actual Tokens Received — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`)

---

### Summary

Both ckERC20 helper contracts emit the deposit event using the caller-supplied `amount` parameter rather than the actual token balance change at the minter's Ethereum address. The IC ckETH minter scrapes these logs and mints ckERC20 tokens equal to the logged `amount`. For fee-on-transfer ERC20 tokens, the minter's Ethereum address receives fewer tokens than `amount`, causing the minter to mint more ckERC20 than it holds in backing ERC20, breaking the 1:1 conservation invariant.

---

### Finding Description

**Vulnerable Solidity code — `CkErc20Deposit.deposit()` (legacy helper):**

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

**Vulnerable Solidity code — `CkDeposit.depositErc20()` (subaccount helper):**

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    ...
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
``` [2](#0-1) 

In both cases, the event is emitted with the **input `amount`**, not the actual balance delta at `minterAddress`. For a fee-on-transfer ERC20 token, `safeTransferFrom` succeeds but the minter receives `amount - fee`. The emitted event still records `amount`.

**IC minter log parsing — `ReceivedErc20LogParser::parse_log()`:**

The IC minter reads the `value` field directly from the event log data:

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
...
value: Erc20Value::from_be_bytes(value_bytes),
``` [3](#0-2) 

The same pattern applies to `ReceivedEthOrErc20LogParser`: [4](#0-3) 

**IC minter minting — `mint()` in `deposit.rs`:**

The minter then calls the ICRC-1 ledger to mint exactly `event.value()` — the logged amount, not the actual received amount:

```rust
client.transfer(TransferArg {
    ...
    amount: event.value(),
})
``` [5](#0-4) 

The `value()` method returns the value parsed from the Ethereum event log, which for fee-on-transfer tokens is inflated relative to what the minter's Ethereum address actually holds: [6](#0-5) 

---

### Impact Explanation

**Vulnerability type:** Chain-fusion mint/burn/replay bug / Ledger conservation bug.

For any fee-on-transfer ERC20 token added to the ckERC20 system:

- The minter's Ethereum address receives `amount - fee` tokens.
- The IC minter mints `amount` ckERC20 tokens to the depositor.
- The ckERC20 total supply grows faster than the minter's ERC20 backing.
- When users redeem ckERC20 for ERC20, the minter will eventually be unable to fulfill withdrawals, as its ERC20 balance is insufficient to cover all outstanding ckERC20.

Additionally, for tokens like Compound V3's cUSDCV3 that interpret `type(uint256).max` as "transfer entire balance": a user could pass `type(uint256).max` as `amount`. The `safeTransferFrom` would transfer only the user's actual balance, but the event would log `type(uint256).max`. The IC minter would attempt to mint `type(uint256).max` ckERC20, which would cause the ICRC-1 ledger's `token_pool` subtraction to panic (underflow protection):

```rust
self.token_pool = self
    .token_pool
    .checked_sub(&amount)
    .expect("total token supply exceeded");
``` [7](#0-6) 

This would trap the minting call, quarantine the deposit event, and permanently lock the user's ERC20 tokens in the minter's Ethereum address with no ckERC20 minted — a loss of user funds.

**Impact: High** — Conservation invariant broken; user funds can be permanently locked or the minter can be drained of backing assets.

---

### Likelihood Explanation

**Likelihood: Low.**

The ckERC20 system only supports ERC20 tokens explicitly whitelisted via NNS governance proposals. Current supported tokens (USDC, USDT, etc.) are standard ERC20 tokens without fee-on-transfer behavior. The vulnerability would only be triggered if a fee-on-transfer or max-transfer token were added via governance. However, the code contains no guard against such tokens being added in the future, and the documentation does not warn governance participants about this class of token.

---

### Recommendation

1. **In the Solidity helper contracts:** Measure the actual balance change at `minterAddress` before and after `safeTransferFrom`, and emit the delta rather than the input `amount`:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

2. **In the IC minter / governance process:** Document that fee-on-transfer and max-transfer ERC20 tokens must not be added to the supported token list, and add a validation step in the NNS proposal process for new ckERC20 tokens.

---

### Proof of Concept

1. A fee-on-transfer ERC20 token (e.g., one that takes a 1% fee on every transfer) is added to the ckERC20 system via NNS governance.
2. Attacker calls `depositErc20(feeToken, 1_000_000, principal, subaccount)` on the `CkDeposit` helper.
3. `safeTransferFrom` executes: minter's Ethereum address receives `990_000` tokens (1% fee deducted).
4. Helper emits `ReceivedEthOrErc20(feeToken, attacker, 1_000_000, principal, subaccount)`.
5. IC minter scrapes the log, reads `value = 1_000_000`, and mints `1_000_000` ckFeeToken to the attacker.
6. Attacker now holds `1_000_000` ckFeeToken backed by only `990_000` real tokens.
7. Repeated deposits inflate the ckFeeToken supply beyond the minter's ERC20 holdings.
8. When legitimate users attempt to redeem ckFeeToken for ERC20, the minter's Ethereum address runs out of backing tokens, and withdrawals fail — draining the protocol.

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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L127-149)
```rust
        let [value_bytes, subaccount_bytes] =
            parse_hex_into_32_byte_words(entry.data, event_source)?;
        let subaccount = LedgerSubaccount::from_bytes(subaccount_bytes);
        let EventSource {
            transaction_hash,
            log_index,
        } = event_source;

        if erc20_contract_address == Address::ZERO {
            let value = Wei::from_be_bytes(value_bytes);
            return Ok(ReceivedEthEvent {
                transaction_hash,
                block_number,
                log_index,
                from_address,
                value,
                principal,
                subaccount,
            }
            .into());
        }

        let value = Erc20Value::from_be_bytes(value_bytes);
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

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L150-155)
```rust
        self.token_pool = self
            .token_pool
            .checked_sub(&amount)
            .expect("total token supply exceeded");
        self.credit(to, amount);
        Ok(())
```
