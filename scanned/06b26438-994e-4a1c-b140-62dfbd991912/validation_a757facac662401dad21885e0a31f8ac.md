### Title
No Mechanism to Remove a Supported ckERC20 Token Allows Fee-on-Transfer ERC-20 to Over-Mint ckERC20 - (`rs/ethereum/cketh/minter/src/main.rs`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter exposes `add_ckerc20_token` to register new ERC-20 tokens but provides no corresponding removal endpoint. The deposit minting logic unconditionally mints `event.value()` — the amount recorded in the Ethereum event log — without verifying the actual ERC-20 balance received by the minter address. If a previously fee-free ERC-20 token (e.g., USDT) later activates a fee-on-transfer, every subsequent deposit will mint more ckERC20 than the minter actually holds, permanently breaking the 1:1 backing invariant with no protocol-level way to halt it.

---

### Finding Description

**Root cause 1 — No removal mechanism:**

The minter's `add_ckerc20_token` endpoint permanently inserts a token into `ckerc20_tokens`:

```rust
// rs/ethereum/cketh/minter/src/main.rs
#[update]
async fn add_ckerc20_token(erc20_token: AddCkErc20Token) {
    ...
    mutate_state(|s| process_event(s, EventType::AddedCkErc20Token(ckerc20_token)));
}
```

The `EventType` enum contains `AddedCkErc20Token` but no `RemovedCkErc20Token`. The Candid interface (`cketh_minter.did`) exposes no `remove_ckerc20_token` method. Once a token is added, it cannot be removed.

**Root cause 2 — Deposit mints event-logged amount, not actual received amount:**

The Ethereum helper contracts emit the *requested* transfer amount, not the *actual* received amount:

```solidity
// rs/ethereum/cketh/minter/ERC20DepositHelper.sol
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
// ^^^ `amount` is the requested value; for fee-on-transfer tokens,
//     minter receives `amount - fee` but event records `amount`
```

The minter scrapes these logs and mints exactly `event.value()`:

```rust
// rs/ethereum/cketh/minter/src/deposit.rs
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),  // <-- event-logged amount, not actual received
    })
    .await
```

For a fee-on-transfer ERC-20, `event.value()` = `amount` while the minter's actual ERC-20 balance increases by only `amount - fee`. The minter mints the full `amount` of ckERC20 tokens, creating unbacked supply.

---

### Impact Explanation

Every deposit of a fee-on-transfer ERC-20 token mints `fee` excess ckERC20 tokens. Over time, the total ckERC20 supply exceeds the minter's actual ERC-20 holdings. When users attempt to withdraw ckERC20 back to ERC-20, the minter's ERC-20 balance is insufficient to fulfill all outstanding ckERC20 tokens. The last withdrawers cannot redeem their ckERC20 at par, resulting in direct loss of funds. The minter's internal `erc20_balances` accounting (which is also updated from event values) will diverge from the true on-chain balance, masking the deficit.

---

### Likelihood Explanation

USDT (Tether) is a real-world ERC-20 with a dormant fee-on-transfer mechanism that its issuer can activate at any time. ckUSDT is a supported ckERC20 token. Any user can trigger the vulnerability simply by depositing USDT after the fee is activated — no special privilege is required. The attacker-controlled entry path is: call `depositErc20` on the helper contract with a fee-on-transfer ERC-20 → minter scrapes the log → minter mints excess ckERC20 → repeat until the minter's ERC-20 reserve is drained by early withdrawers.

---

### Recommendation

1. **Add a `remove_ckerc20_token` endpoint** (restricted to the orchestrator) that adds a new `RemovedCkErc20Token` event type and removes the token from `ckerc20_tokens`, preventing new deposits from being accepted for that token.

2. **Verify actual received balance** in the deposit flow by comparing the minter's ERC-20 balance before and after the `transferFrom` call (or by using a balance-check approach in the helper contract), and mint only the delta rather than the event-logged amount.

---

### Proof of Concept

1. ckUSDT is added as a supported token via `add_ckerc20_token` (orchestrator-gated, already done on mainnet).
2. USDT activates its fee-on-transfer (e.g., 1% fee).
3. A user calls `depositErc20(usdt_address, 1_000_000, principal, subaccount)` on the helper contract.
4. The helper calls `safeTransferFrom(user, minter, 1_000_000)` — minter receives `990_000` USDT.
5. The helper emits `ReceivedErc20(usdt_address, user, 1_000_000, principal)`.
6. The minter scrapes the log, reads `value = 1_000_000`, and mints `1_000_000` ckUSDT to the user.
7. The minter holds `990_000` USDT but has issued `1_000_000` ckUSDT — a `10_000` unit deficit per deposit.
8. Since there is no `remove_ckerc20_token`, this continues indefinitely.
9. Eventually, the minter's USDT reserve is exhausted; the last ckUSDT holders cannot redeem.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol (L519-531)
```text
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
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L398-424)
```rust
    pub fn record_add_ckerc20_token(&mut self, ckerc20_token: CkErc20Token) {
        assert_eq!(
            self.ethereum_network, ckerc20_token.erc20_ethereum_network,
            "ERROR: Expected {}, but got {}",
            self.ethereum_network, ckerc20_token.erc20_ethereum_network
        );
        let ckerc20_with_same_symbol = self
            .supported_ck_erc20_tokens()
            .filter(|ckerc20| ckerc20.ckerc20_token_symbol == ckerc20_token.ckerc20_token_symbol)
            .collect::<Vec<_>>();
        assert_eq!(
            ckerc20_with_same_symbol,
            vec![],
            "ERROR: ckERC20 token symbol {} is already used by {:?}",
            ckerc20_token.ckerc20_token_symbol,
            ckerc20_with_same_symbol
        );
        assert_eq!(
            self.ckerc20_tokens.try_insert(
                ckerc20_token.ckerc20_ledger_id,
                ckerc20_token.erc20_contract_address,
                ckerc20_token.ckerc20_token_symbol,
            ),
            Ok(()),
            "ERROR: some ckERC20 tokens use the same ckERC20 ledger ID or ERC-20 address"
        );
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L600-612)
```text
type AddCkErc20Token = record {
    // Ethereum chain ID.
    chain_id : nat;

    // The Ethereum address of the ERC-20 smart contract.
    address : text;

    // The ckERC20 token symbol on the ledger.
    ckerc20_token_symbol : text;

    // The ledger ID for that ckERC20 token.
    ckerc20_ledger_id : principal;
};
```
