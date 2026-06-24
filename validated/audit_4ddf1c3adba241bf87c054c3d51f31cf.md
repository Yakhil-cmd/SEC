Audit Report

## Title
Unsupported ERC-20 Tokens Deposited via `depositErc20` Are Permanently Irrecoverable at the ckETH Minter Address - (File: rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol, rs/ethereum/cketh/minter/src/eth_logs/scraping.rs)

## Summary
The `depositErc20` function in `DepositHelperWithSubaccount.sol` accepts any ERC-20 token address and unconditionally transfers it to the minter's Ethereum address, while the minter's log scraping infrastructure only fetches `ReceivedEthOrErc20` events whose token address matches a currently supported ckERC20 token. Any deposit of an unsupported token is silently dropped at the RPC filter level: no ckERC20 is minted, no refund is issued, and no canister endpoint exists to recover the stranded tokens. The result is a permanent, irreversible loss of the deposited ERC-20 value.

## Finding Description
**Deposit path — no whitelist enforcement:**

`DepositHelperWithSubaccount.sol` L511–532 performs only a zero-address check before calling `safeTransferFrom` to the minter address and emitting `ReceivedEthOrErc20`. There is no check against any on-chain or off-chain list of supported tokens:

```solidity
function depositErc20(address erc20Address, uint256 amount, bytes32 principal, bytes32 subaccount) public {
    require(erc20Address != ZERO_ADDRESS, "ERC20: depositErc20 from the zero address");
    IERC20 erc20Token = IERC20(erc20Address);
    erc20Token.safeTransferFrom(msg.sender, minterAddress, amount);
    emit ReceivedEthOrErc20(erc20Address, msg.sender, amount, principal, subaccount);
}
```

**Log scraping path — strict whitelist at the RPC filter level:**

`ReceivedEthOrErc20LogScraping::next_scrape` (scraping.rs L100–121) builds the `eth_getLogs` topic filter as the zero address (ETH) unioned with only the addresses of currently supported ckERC20 tokens via `erc20_smart_contracts_addresses_as_topics`. Any `ReceivedEthOrErc20` event whose token address is not in `state.ckerc20_tokens` is never returned by the RPC call and is therefore never processed by the minter. The same restriction applies to `ReceivedErc20LogScraping::next_scrape` (L70–91).

**No recovery path:**

The `withdraw_erc20` endpoint (main.rs L418–428) calls `find_ck_erc20_token_by_ledger_id` and returns `TokenNotSupported` for any unrecognized ledger ID. No admin, governance, or emergency endpoint exists to issue an Ethereum transaction that would transfer stranded ERC-20 tokens out of the minter address.

**Root cause:** The deposit contract is permissive (accepts any ERC-20), while the minter's observation and withdrawal layers are restrictive (only supported tokens). The asymmetry creates a one-way valve: tokens enter but cannot exit for unsupported contracts.

## Impact Explanation
Any ERC-20 tokens transferred to the minter's Ethereum address via `depositErc20` for an unsupported token contract are permanently locked. The minter never observes the deposit, never mints ckERC20, and cannot issue a refund. This is a concrete, irreversible loss of user funds in an in-scope ckERC20/Chain Fusion financial integration component, matching the allowed High impact: *"Significant Chain Fusion, ck-token, ledger… security impact with concrete user or protocol harm."* Depending on the value of the deposited tokens, it could also reach the Critical threshold for permanent loss of in-scope chain-key/ledger assets.

## Likelihood Explanation
The attack path requires no privileges: any Ethereum user who holds an ERC-20 token and has approved the helper contract can trigger the loss by calling `depositErc20`. Realistic scenarios include depositing a token that was previously supported and later removed from the minter's list, using the wrong contract address, or depositing a token not yet added to the supported list. The `safeTransferFrom` call succeeds silently, giving the user no on-chain indication that the deposit will never be processed.

## Recommendation
Enforce the supported-token check at the point of deposit in the Solidity contract. The helper contract should maintain (or query) a list of supported ERC-20 addresses and revert `depositErc20` if `erc20Address` is not in that list. Alternatively, add a governance-controlled rescue function in the minter canister that can construct and sign an Ethereum transaction to transfer any ERC-20 token balance held at the minter address to a designated recovery address, providing a fallback for tokens that slip through before the on-chain check is added.

## Proof of Concept
1. Call `get_minter_info` on the ckETH minter canister to obtain `deposit_with_subaccount_helper_contract_address` and `supported_ckerc20_tokens`.
2. Select any ERC-20 token whose contract address is absent from `supported_ckerc20_tokens`.
3. Call `approve(helper_contract_address, amount)` on that ERC-20 contract from a test account.
4. Call `depositErc20(unsupported_erc20_address, amount, principal, subaccount)` on the helper contract. `safeTransferFrom` succeeds; tokens move to the minter's Ethereum address; `ReceivedEthOrErc20` is emitted.
5. Observe that `ReceivedEthOrErc20LogScraping::next_scrape` constructs a topic filter containing only `[0x00…00] ∪ {supported_token_addresses}`. The unsupported token address is absent; the RPC `eth_getLogs` call never returns the event.
6. Confirm no ckERC20 is minted and no refund is issued. Query the minter's `withdraw_erc20` endpoint with any ledger ID for the unsupported token; it returns `TokenNotSupported`. The tokens remain permanently locked at the minter's Ethereum address.