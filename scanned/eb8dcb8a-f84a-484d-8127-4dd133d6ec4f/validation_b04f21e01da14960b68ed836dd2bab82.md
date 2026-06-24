### Title
Unsupported ERC-20 Token Deposits Permanently Stuck in ckETH Minter's Ethereum Address — (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

### Summary
The ckETH minter canister only processes ERC-20 deposit events for tokens explicitly added as supported ckERC20 tokens via NNS proposal. The helper smart contract (`ERC20DepositHelper.sol`) accepts any ERC-20 token without validation. When an unsupported ERC-20 token is deposited via the helper contract, the tokens are transferred to the minter's threshold-ECDSA-controlled Ethereum address, but the minter never processes the deposit and never mints any ckERC20 tokens. There is no recovery mechanism in the minter canister for these stuck tokens — recovery requires a minter canister upgrade via NNS governance.

### Finding Description
The helper smart contract unconditionally accepts any ERC-20 token from any caller:

```solidity
function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
    IERC20 erc20Token = IERC20(erc20_address);
    erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);
    emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
}
``` [1](#0-0) 

The minter's log-scraping logic only fetches logs for supported ERC-20 tokens. Deposits of unsupported tokens are silently ignored — no `AcceptedErc20Deposit` event is emitted, no ckERC20 tokens are minted, and no refund is issued. The minting path in `deposit.rs` panics if an unsupported token somehow reaches it, but this is guarded by the `QuarantinedDeposit` scope guard, which marks the event as quarantined and prevents any future processing:

```rust
ReceivedEvent::Erc20(event) => {
    if let Some(result) = read_state(|s| {
        s.ckerc20_tokens
            .get_entry_alt(&event.erc20_contract_address)
            .map(|(principal, symbol)| (symbol.to_string(), *principal))
    }) {
        result
    } else {
        panic!(
            "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. \
             (This should have already been filtered out by process_event)"
        )
    }
}
``` [2](#0-1) 

The documentation explicitly acknowledges this gap:

> "Note that the helper smart contract does not enforce any whitelist of allowed ERC-20 tokens. This is enforced by the minter, which fetches logs only for the supported ERC-20 tokens. Therefore, funds of unsupported ERC-20 tokens could be deposited via the helper smart contract, but the minter will not know anything about it." [3](#0-2) 

The integration test `should_fail_to_mint_from_unsupported_erc20_contract_address` confirms that no `AcceptedErc20Deposit` event is emitted and no mint occurs for unsupported tokens, meaning the ERC-20 tokens are silently absorbed into the minter's Ethereum address with no on-chain record of the deposit on the IC side. [4](#0-3) 

The minter's Ethereum address is controlled exclusively via threshold ECDSA by the minter canister. To issue a recovery transaction (e.g., transfer the stuck ERC-20 tokens to the depositor), the minter canister would need to be upgraded via NNS governance to add a recovery endpoint.

### Impact Explanation
Any ERC-20 tokens deposited via the helper contract for an unsupported token type are permanently stuck in the minter's Ethereum address. The user receives no ckERC20 tokens and has no on-chain mechanism to recover their funds. Recovery requires an NNS governance proposal to upgrade the minter canister with a new recovery function. This is a **chain-fusion ledger conservation bug**: real ERC-20 value is absorbed by the IC chain-fusion system with no corresponding IC-side credit and no refund path.

### Likelihood Explanation
Any unprivileged user can trigger this by calling `depositErc20` on the helper contract with any ERC-20 token address. This can happen by mistake (user deposits the wrong token, e.g., a token that was previously supported but later removed, or a token they believe is supported) or intentionally. The helper contract is publicly callable on Ethereum mainnet. The documentation warning is easy to miss, and there is no on-chain enforcement.

### Recommendation
1. **Short-term**: Add a recovery endpoint to the minter canister (callable by the minter's controller/NNS) that can issue an Ethereum transaction to transfer unsupported ERC-20 tokens from the minter's address to a specified destination.
2. **Long-term**: Enforce a token whitelist in the helper smart contract so that `depositErc20` reverts for unsupported ERC-20 contract addresses. This mirrors the fix applied in the referenced external report (PR #515), where the `refund` function was made to revert for tokens that do not meet the required criteria.

### Proof of Concept
1. Identify an ERC-20 token not in the minter's `supported_ckerc20_tokens` list (query `get_minter_info`).
2. Call `approve(helper_contract_address, amount)` on the unsupported ERC-20 contract.
3. Call `deposit(unsupported_erc20_address, amount, encoded_principal)` on the `CkErc20Deposit` helper contract.
4. The helper contract executes `safeTransferFrom(msg.sender, cketh_minter_main_address, amount)` — the ERC-20 tokens are now in the minter's Ethereum address.
5. The minter's log scraping never fetches logs for this token's contract address; no `AcceptedErc20Deposit` event is recorded; no ckERC20 tokens are minted.
6. The depositor's ERC-20 tokens are permanently stuck in the minter's Ethereum address with no IC-side record and no refund mechanism.

### Citations

**File:** rs/ethereum/cketh/minter/ERC20DepositHelper.sol (L498-503)
```text
    function deposit(address erc20_address, uint256 amount, bytes32 principal) public {
        IERC20 erc20Token = IERC20(erc20_address);
        erc20Token.safeTransferFrom(msg.sender, cketh_minter_main_address, amount);

        emit ReceivedErc20(erc20_address, msg.sender, amount, principal);
    }
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L53-67)
```rust
        let (token_symbol, ledger_canister_id) = match &event {
            ReceivedEvent::Eth(_) => ("ckETH".to_string(), eth_ledger_canister_id),
            ReceivedEvent::Erc20(event) => {
                if let Some(result) = read_state(|s| {
                    s.ckerc20_tokens
                        .get_entry_alt(&event.erc20_contract_address)
                        .map(|(principal, symbol)| (symbol.to_string(), *principal))
                }) {
                    result
                } else {
                    panic!(
                        "Failed to mint ckERC20: {event:?} Unsupported ERC20 contract address. (This should have already been filtered out by process_event)"
                    )
                }
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

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L1613-1639)
```rust
#[test]
fn should_fail_to_mint_from_unsupported_erc20_contract_address() {
    let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
    let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
    let unsupported_erc20_address: Address = "0x6b175474e89094c44da98b954eedeac495271d0f"
        .parse()
        .unwrap();
    assert!(
        !ckerc20
            .supported_erc20_contract_addresses()
            .contains(&unsupported_erc20_address)
    );

    ckerc20
        .deposit(DepositCkErc20Params::new(
            ONE_USDC,
            CkErc20Token {
                erc20_contract_address: unsupported_erc20_address.to_string(),
                ..ckusdc.clone()
            },
        ))
        .expect_no_mint()
        .check_events()
        .assert_has_no_event_satisfying(|event| {
            matches!(event, EventPayload::AcceptedErc20Deposit { .. })
        });
}
```
