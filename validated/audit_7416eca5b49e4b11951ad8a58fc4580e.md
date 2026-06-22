### Title
`depositErc20()` Does Not Verify Actual Tokens Received Before Emitting Deposit Event — (`File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`)

---

### Summary

The `depositErc20()` function in the `CkDeposit` helper smart contract calls `safeTransferFrom()` to pull ERC-20 tokens from the user to the minter address, then immediately emits a `ReceivedEthOrErc20` event using the caller-supplied `amount` parameter — without checking the minter's actual token balance before and after the transfer. The ckETH minter canister on the Internet Computer scrapes this event and mints ckERC-20 tokens 1:1 against the `amount` field in the event. For fee-on-transfer (deflationary) ERC-20 tokens, the minter receives fewer tokens than `amount`, yet mints the full `amount` of ckERC-20, breaking the 1:1 backing invariant.

---

### Finding Description

In `DepositHelperWithSubaccount.sol`, the `depositErc20()` function:

```solidity
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
        amount,       // <-- uses caller-supplied amount, not actual received amount
        principal,
        subaccount
    );
}
```

The `amount` emitted in the `ReceivedEthOrErc20` event is the parameter passed by the caller, not the actual amount received by `minterAddress`. For fee-on-transfer tokens, the minter address receives `amount - fee`, but the event records `amount`. The IC minter canister scrapes this event and mints exactly `event.value()` ckERC-20 tokens to the beneficiary. [1](#0-0) 

The IC minter's `mint()` call in `rs/ethereum/cketh/minter/src/deposit.rs` uses `event.value()` directly as the mint amount, which is sourced from the `amount` field of the `ReceivedEthOrErc20` log event: [2](#0-1) 

The same pattern exists in the older `ERC20DepositHelper.sol` (`CkErc20Deposit.deposit()`): [3](#0-2) 

---

### Impact Explanation

For any fee-on-transfer ERC-20 token that is added as a supported ckERC-20 token, every deposit mints more ckERC-20 than the minter actually holds in ERC-20 collateral. Over time, the total ckERC-20 supply exceeds the actual ERC-20 balance held by the minter's Ethereum address. When users attempt to withdraw ckERC-20 back to ERC-20, the minter will be unable to fulfill all withdrawal requests — the last withdrawers receive nothing. This is a **ledger conservation bug** / **chain-fusion mint/burn accounting bug**: ckERC-20 tokens are minted without full 1:1 ERC-20 backing, breaking the core security guarantee of the twin-token protocol.

---

### Likelihood Explanation

The minter currently enforces a whitelist of supported ERC-20 tokens via `s.ckerc20_tokens`. If a fee-on-transfer token is never added to the whitelist, the bug is not exploitable in practice. However:

1. The whitelist is governed by NNS proposals, and any future addition of a deflationary/fee-on-transfer ERC-20 token (e.g., PAXG, STA, or similar) would immediately trigger the vulnerability.
2. The helper contract itself has no enforcement — it accepts any `erc20Address` — so the vulnerability is structurally present in the on-chain code regardless of the current whitelist state.
3. The minter's own documentation warns that the helper contract does not enforce a whitelist of allowed ERC-20 tokens. [4](#0-3) 

---

### Recommendation

In `depositErc20()`, record the minter's ERC-20 balance before and after the `safeTransferFrom()` call, and emit the actual received amount (the difference) rather than the caller-supplied `amount`:

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

    emit ReceivedEthOrErc20(
        erc20Address,
        msg.sender,
        actualReceived,   // actual amount received, not caller-supplied amount
        principal,
        subaccount
    );
}
```

Apply the same fix to `ERC20DepositHelper.sol`'s `deposit()` function. Additionally, consider enforcing at the minter level that only non-fee-on-transfer tokens are added to the supported ckERC-20 whitelist.

---

### Proof of Concept

1. A fee-on-transfer ERC-20 token `FeeToken` (e.g., 1% fee on every transfer) is added to the ckERC-20 supported list via NNS governance.
2. Alice calls `depositErc20(FeeToken, 1000, alicePrincipal, 0x00)` on the `CkDeposit` helper contract.
3. `safeTransferFrom` transfers 1000 tokens from Alice; due to the 1% fee, `minterAddress` receives only 990 tokens.
4. The helper emits `ReceivedEthOrErc20(FeeToken, Alice, 1000, alicePrincipal, 0x00)` — recording `amount = 1000`.
5. The IC minter scrapes the event and calls `client.transfer(... amount: Nat::from(1000) ...)` on the ckERC-20 ledger, minting 1000 ckFeeToken to Alice.
6. The minter's Ethereum address only holds 990 FeeToken, but 1000 ckFeeToken are in circulation.
7. Repeated deposits inflate the ckERC-20 supply beyond the actual ERC-20 collateral, eventually making the protocol insolvent for that token. [5](#0-4) [6](#0-5)

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

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L69-102)
```rust
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

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
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
