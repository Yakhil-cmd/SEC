### Title
ckERC20 Minter Over-Mints Tokens for Fee-on-Transfer ERC20 Deposits — (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol)

---

### Summary

The ckERC20 deposit helper smart contracts emit the `ReceivedEthOrErc20` event using the caller-supplied `amount` parameter rather than the actual tokens received by the minter address. The IC ckETH minter scrapes these events and mints exactly `event.value()` ckERC20 tokens. For fee-on-transfer ERC20 tokens (e.g., USDT with its fee mechanism enabled), the minter receives `amount − fee` tokens on Ethereum but mints `amount` ckERC20 on the IC, permanently breaking the 1:1 backing invariant.

---

### Finding Description

**Root cause — Solidity helper contract (`DepositHelperWithSubaccount.sol`):**

```solidity
function depositErc20(
    address erc20Address,
    uint256 amount,
    bytes32 principal,
    bytes32 subaccount
) public {
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(
        msg.sender,
        minterAddress,
        amount          // ← for fee-on-transfer tokens, minter receives amount − fee
    );

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        amount,         // ← emits the REQUESTED amount, not the RECEIVED amount
        principal,
        subaccount
    );
}
``` [1](#0-0) 

The same pattern exists in the older helper: [2](#0-1) 

**Root cause — IC minter mints from event value:**

The minter scrapes the `ReceivedEthOrErc20` log, parses the `uint256 amount` field directly into `ReceivedErc20Event.value`, and passes it unchanged to the ICRC-1 ledger mint call:

```rust
let block_index = match client
    .transfer(TransferArg {
        ...
        amount: event.value(),   // ← value from the Ethereum log, not verified against actual balance
    })
    .await
``` [3](#0-2) 

The `value()` method returns the raw `amount` field from the event without any cross-check against the minter's actual ERC20 balance change: [4](#0-3) 

The log parser reads the `amount` word directly from the event data: [5](#0-4) 

There is no step anywhere in the deposit pipeline that compares `event.value()` against the actual ERC20 balance delta of the minter address.

---

### Impact Explanation

**Vulnerability class:** Chain-fusion mint/burn/replay bug — ledger conservation invariant broken.

For every deposit of a fee-on-transfer ERC20 token, the minter mints `amount` ckERC20 but only holds `amount − fee` ERC20 as backing. The cumulative shortfall grows with each deposit. When users attempt to withdraw ckERC20 back to ERC20, the minter's Ethereum address will eventually lack sufficient ERC20 to honor all redemptions. The last withdrawers lose funds proportional to the accumulated fee shortfall. The ckERC20 token's 1:1 peg to its underlying ERC20 is permanently broken.

USDT (Tether), which is a supported ckERC20 token, contains a fee-on-transfer mechanism that is currently set to zero but is activatable by the Tether contract owner at any time — exactly the scenario described in the reference report. Any future supported ERC20 token with a non-zero transfer fee would trigger this immediately upon listing.

---

### Likelihood Explanation

**Medium.** The currently supported ckERC20 tokens (USDC, USDT, etc.) have their transfer fees set to zero today, so the bug is latent rather than immediately active. However:

1. USDT's fee mechanism is live on-chain and can be enabled by the Tether issuer without any IC governance action.
2. The ckERC20 token list is expanded via NNS proposals; any future token with a non-zero transfer fee would be immediately exploitable.
3. No privileged access is required — any user making a standard `depositErc20` call triggers the over-minting.

---

### Recommendation

**Short term:** Document that fee-on-transfer and rebasing ERC20 tokens are not supported by the ckERC20 system, and add an explicit check in the NNS proposal review process for new ckERC20 tokens.

**Long term:** Modify the helper contract to measure the actual balance delta of `minterAddress` before and after the `safeTransferFrom` call, and emit that delta as the `amount` in the event:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 received = erc20Token.balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, received, principal, subaccount);
```

This ensures the IC minter mints only what was actually received, regardless of the token's transfer mechanics.

---

### Proof of Concept

1. USDT enables its transfer fee (e.g., 1 basis point = 0.01%).
2. Alice calls `depositErc20(USDT_ADDRESS, 1_000_000 USDT, alice_principal, 0x00)` on the `CkDeposit` helper contract.
3. `safeTransferFrom` transfers 1,000,000 USDT from Alice to the minter; due to the 0.01% fee, the minter receives 999,900 USDT.
4. The helper emits `ReceivedEthOrErc20(..., 1_000_000, ...)`.
5. The IC minter scrapes the event, reads `value = 1_000_000`, and mints 1,000,000 ckUSDT to Alice.
6. Alice holds 1,000,000 ckUSDT; the minter holds only 999,900 USDT.
7. After many such deposits, the minter's USDT reserve is insufficient to honor all ckUSDT redemptions. The last redeemers receive nothing.

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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
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

**File:** rs/ethereum/cketh/minter/src/eth_logs/parser.rs (L127-160)
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
        Ok(ReceivedErc20Event {
            transaction_hash,
            block_number,
            log_index,
            from_address,
            value,
            principal,
            erc20_contract_address,
            subaccount,
        }
        .into())
```
