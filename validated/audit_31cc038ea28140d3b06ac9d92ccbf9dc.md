Audit Report

## Title
Fee-on-Transfer ERC-20 Token Causes ckERC20 Over-Minting (Chain-Fusion Ledger Conservation Bug) - (File: `rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/ERC20DepositHelper.sol`, `rs/ethereum/cketh/minter/src/deposit.rs`)

## Summary
Both `DepositHelperWithSubaccount.sol::depositErc20` and `ERC20DepositHelper.sol::deposit` emit their respective deposit events using the caller-supplied `amount` parameter without verifying the actual balance received by the minter address after `safeTransferFrom`. The IC minter canister reads `event.value()` verbatim from the scraped Ethereum log and mints that exact quantity of ckERC20 on the IC ledger. For any fee-on-transfer ERC-20 token approved via NNS governance, this permanently breaks the 1:1 backing invariant and causes protocol insolvency.

## Finding Description
In `DepositHelperWithSubaccount.sol` lines 519–531, `safeTransferFrom(msg.sender, minterAddress, amount)` is called and then `ReceivedEthOrErc20(..., amount, ...)` is emitted using the original `amount` argument with no balance-before/after check: [1](#0-0) 

The same pattern exists in `ERC20DepositHelper.sol` lines 498–502: [2](#0-1) 

On the IC side, `deposit.rs` lines 73–81 reads `event.value()` directly from the scraped log and passes it as the `amount` to the ICRC-1 ledger `transfer` call, with no cross-check against the minter's actual on-chain ERC-20 balance: [3](#0-2) 

The `value` field of `ReceivedErc20Event` is populated verbatim from the Ethereum log: [4](#0-3) 

For a fee-on-transfer token (e.g., one deducting 1% per `transferFrom`), a deposit of `1000e18` causes the minter to receive `990e18` while the IC ledger mints `1000e18` ckERC20. No existing guard in either the Solidity contracts or the Rust minter detects or corrects this discrepancy.

## Impact Explanation
This constitutes **illegal minting** of ckERC20 tokens in excess of the ERC-20 collateral held by the minter address, directly matching the Critical allowed impact: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets."* Repeated deposits drain the backing reserve; once the minter's ERC-20 balance is exhausted, withdrawal requests revert, making the last holders of ckERC20 unable to redeem — a full protocol insolvency. The ckERC20 system is explicitly listed as in-scope.

## Likelihood Explanation
The precondition is NNS governance approval of a fee-on-transfer ERC-20 token. This is a realistic governance event: USDT carries a dormant fee mechanism (currently set to zero) that its issuer can activate unilaterally, and future NNS proposals may add tokens with active transfer fees. Once such a token is approved, **any unprivileged Ethereum user** can call `depositErc20` or `deposit` to trigger the over-minting — no special access, no victim interaction, and no threshold corruption required. The attack is repeatable indefinitely until the minter's ERC-20 balance is fully drained.

## Recommendation
In both `depositErc20` (`DepositHelperWithSubaccount.sol`) and `deposit` (`ERC20DepositHelper.sol`), record the minter's ERC-20 balance before and after `safeTransferFrom` and emit the **actual received amount** (the difference) rather than the caller-supplied `amount`:

```solidity
uint256 balanceBefore = erc20Token.balanceOf(minterAddress);
erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
uint256 actualReceived = erc20Token.balanceOf(minterAddress) - balanceBefore;
require(actualReceived > 0, "ERC20: zero amount received");
emit ReceivedEthOrErc20(erc20Address, msg.sender, actualReceived, principal, subaccount);
```

This ensures the IC minter mints only what was actually received, preserving the 1:1 backing invariant regardless of the underlying token's fee behavior.

## Proof of Concept
1. Submit an NNS proposal to add a fee-on-transfer ERC-20 token (1% fee per `transferFrom`) as a supported ckERC20 token; proposal passes via normal governance.
2. Alice calls `depositErc20(tokenAddress, 1000e18, alicePrincipal, 0x0)` on the `CkDeposit` helper contract.
3. The ERC-20 contract deducts 1%: minter receives `990e18`; the helper emits `ReceivedEthOrErc20(..., 1000e18, ...)`.
4. The IC minter scrapes the log, reads `value = 1000e18` from `ReceivedErc20Event.value`, and calls `ICRC1Client.transfer(amount: 1000e18)` on the ckERC20 ledger.
5. Alice holds `1000e18` ckERC20; minter holds only `990e18` ERC-20.
6. After 100 such deposits: minter holds `99_000e18` ERC-20, ckERC20 total supply is `100_000e18`.
7. The last ~1% of ckERC20 holders cannot redeem; withdrawal transactions revert due to insufficient ERC-20 balance.

A deterministic integration test can reproduce this by deploying a mock ERC-20 with a configurable transfer fee, registering it via the minter's NNS-controlled token list, and asserting that `ckERC20.totalSupply() > erc20.balanceOf(minterAddress)` after a single deposit.

### Citations

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
