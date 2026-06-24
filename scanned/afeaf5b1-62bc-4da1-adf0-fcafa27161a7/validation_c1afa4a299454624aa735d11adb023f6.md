### Title
Fee-on-Transfer ERC-20 Tokens Allow ckERC20 Over-Minting, Breaking 1:1 Backing Invariant - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The ckETH minter's ERC-20 deposit helper contracts emit deposit events using the caller-supplied nominal `amount`, not the actual tokens received by the minter address. The IC minter canister reads this event value and mints an equal quantity of ckERC20 tokens. For any ERC-20 token with a fee-on-transfer mechanism, the minter address receives `amount - fee` on Ethereum but mints `amount` ckERC20 on the IC, permanently breaking the 1:1 backing invariant.

---

### Finding Description

The deposit flow for ERC-20 → ckERC20 proceeds as follows:

1. The user calls `depositErc20(erc20Address, amount, principal, subaccount)` on the helper contract.
2. The helper calls `safeTransferFrom(msg.sender, minterAddress, amount)` on the ERC-20 contract.
3. The helper emits `ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount)` — using the input `amount` parameter directly.
4. The IC minter scrapes this log, parses `value` from the event data, and mints exactly `event.value()` ckERC20 tokens to the user.

In `DepositHelperWithSubaccount.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
//                                              ^^^^^^ nominal input, not actual received
``` [1](#0-0) 

The same pattern exists in the legacy `ERC20DepositHelper.sol`:

```solidity
erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
``` [2](#0-1) 

The IC minter's log parser reads the `value` field directly from the event's ABI-encoded `data` field:

```rust
let [value_bytes] = parse_hex_into_32_byte_words(entry.data, event_source)?;
// ...
value: Erc20Value::from_be_bytes(value_bytes),
``` [3](#0-2) 

The minter then mints `event.value()` — the nominal amount from the log — to the user:

```rust
amount: event.value(),
``` [4](#0-3) 

There is no step anywhere in the IC minter that queries the minter's actual ERC-20 balance on Ethereum to reconcile against the minted supply.

---

### Impact Explanation

If a supported ckERC20 token activates a fee-on-transfer mechanism (e.g., USDT's contract has this capability built in), every deposit will mint more ckERC20 than the minter holds in ERC-20. The ckERC20 total supply will exceed the actual ERC-20 collateral held by the minter address. Later withdrawers will find the minter unable to fulfill redemptions — the last users to withdraw will receive less ERC-20 than their ckERC20 represents, or withdrawals will fail entirely. This is a direct ledger conservation break: ckERC20 is no longer backed 1:1 by ERC-20.

---

### Likelihood Explanation

Currently low-to-medium. The supported tokens (ckUSDC, ckUSDT, etc.) do not have active transfer fees today. However, USDT's Ethereum contract explicitly contains a fee mechanism that its issuer (Tether) can activate unilaterally at any time without any on-chain governance vote or IC NNS proposal. The vulnerability is latent and would be triggered automatically and silently the moment any supported token activates its fee, with no action required from an attacker beyond normal deposits.

---

### Recommendation

The helper contract should measure the minter's actual balance before and after the `transferFrom` call and emit only the net received amount:

```solidity
uint256 balanceBefore = IERC20(erc20Address).balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = IERC20(erc20Address).balanceOf(minterAddress) - balanceBefore;
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

This ensures the emitted event value always reflects the actual tokens received, and the IC minter will mint the correct ckERC20 amount regardless of the token's fee behavior. Additionally, the IC minter governance process should require a fee-on-transfer audit before adding any new ERC-20 token as a supported ckERC20 asset.

---

### Proof of Concept

1. Assume USDT activates a 1% transfer fee.
2. User calls `depositErc20(USDT_ADDRESS, 1_000_000 /*1 USDT*/, principal, subaccount)` on `DepositHelperWithSubaccount`.
3. `safeTransferFrom` transfers 1 USDT from user; USDT contract deducts 1% fee → minter address receives 990,000 units; USDT fee collector receives 10,000 units.
4. Helper emits `ReceivedEthOrErc20(..., 1_000_000, ...)` — the original `amount`, not 990,000.
5. IC minter scrapes the log, parses `value = 1_000_000`, and calls `icrc1_transfer` on the ckUSDT ledger to mint 1,000,000 ckUSDT units to the user.
6. Minter holds 990,000 USDT on Ethereum but has issued 1,000,000 ckUSDT on IC.
7. Repeated deposits accumulate the deficit. The last ~1% of ckUSDT holders cannot redeem their tokens. [5](#0-4) [4](#0-3)

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
