### Title
Unsupported ERC-20 Tokens Deposited via Helper Contract to ckETH Minter's Ethereum Address Become Permanently Stuck — (`rs/ethereum/cketh/minter/DepositHelperWithSubaccount.sol`, `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The ckETH minter's Ethereum helper contract (`CkDeposit`) accepts **any** ERC-20 token via `depositErc20()` with no whitelist enforcement, transferring tokens directly to the minter's Ethereum address. The minter canister only scrapes logs and processes deposits for **supported** ERC-20 tokens. If an unsupported ERC-20 token is deposited, the real ERC-20 tokens are held at the minter's Ethereum address permanently, with no on-chain or canister-side mechanism to recover them. The `withdraw_erc20` endpoint on the minter canister explicitly rejects requests for unsupported tokens.

---

### Finding Description

The `CkDeposit` helper contract's `depositErc20` function accepts any ERC-20 contract address and transfers the tokens to the immutable `minterAddress`: [1](#0-0) 

There is no whitelist check on `erc20Address`. The tokens are transferred to `minterAddress` unconditionally. The minter's own documentation acknowledges this: [2](#0-1) 

On the IC side, the minter only scrapes logs for supported ERC-20 tokens. When `withdraw_erc20` is called, it looks up the token by ledger ID and returns `TokenNotSupported` if the token is not in `ckerc20_tokens`: [3](#0-2) 

The minter's DID interface exposes no endpoint to recover arbitrary ERC-20 tokens from its Ethereum address: [4](#0-3) 

The minter's state tracks `erc20_balances` only for supported tokens: [5](#0-4) 

There is no "rescue" or "recover" path for tokens not in `ckerc20_tokens`.

---

### Impact Explanation

**Vulnerability class: chain-fusion token conservation bug.**

Any ERC-20 token deposited via the helper contract to the minter's Ethereum address that is not in the minter's supported token list is permanently locked. The minter's threshold-ECDSA key controls the Ethereum address, but the canister exposes no endpoint to sign an arbitrary ERC-20 `transfer()` call to recover such tokens. The funds are irretrievably lost from the depositor's perspective. This is a direct analog to the Reservoir "mistakenly deposited token" scenario: the minter's Ethereum address acts as the Reservoir, and unsupported ERC-20 tokens are the "wrong token" with no withdrawal path.

---

### Likelihood Explanation

The helper contract is publicly callable by any Ethereum user. The `depositErc20` function accepts any `erc20Address` argument. A user who mistakenly specifies an unsupported ERC-20 contract address (e.g., a newly listed token not yet added to the minter, or a token from a different chain), or who deposits to the wrong helper contract address, will lose their tokens permanently. The documentation warns about this but provides no technical safeguard. Given the number of ERC-20 tokens in existence and the fact that new tokens are added to the minter via NNS proposals over time, the window for mistaken deposits is real and ongoing.

---

### Recommendation

1. Add a canister endpoint (restricted to the NNS or orchestrator) that allows signing an arbitrary ERC-20 `transfer()` call from the minter's Ethereum address to a designated recovery address, for tokens not in `ckerc20_tokens`. This mirrors the Reservoir fix suggestion: implement a withdrawal path for non-native tokens.
2. Alternatively, add a Solidity-level check in `depositErc20` that reverts if `erc20Address` is not in a minter-controlled allowlist (requires a mutable allowlist in the helper contract, which conflicts with the current immutable design).
3. At minimum, emit a clear on-chain revert or warning when an unsupported token is deposited, so users can recover before the transaction finalizes.

---

### Proof of Concept

1. User calls `depositErc20(unsupportedTokenAddress, amount, encodedPrincipal, subaccount)` on the deployed `CkDeposit` helper contract at `0x18901044688D3756C35Ed2b36D93e6a5B8e00E68`.
2. The helper contract calls `transferFrom(user, minterAddress, amount)` on the unsupported ERC-20 contract — succeeds, tokens now held at minter's Ethereum address.
3. The `ReceivedEthOrErc20` event is emitted with `erc20ContractAddress = unsupportedTokenAddress`.
4. The minter's timer scrapes logs but filters only for addresses in `ckerc20_tokens`; the event is silently ignored.
5. The user calls `withdraw_erc20` on the minter canister with any `ckerc20_ledger_id` — receives `TokenNotSupported` error.
6. No other endpoint exists to recover the tokens. The ERC-20 tokens are permanently locked at the minter's Ethereum address. [1](#0-0) [3](#0-2) [6](#0-5)

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L418-428)
```rust
    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L412-415)
```text
type WithdrawErc20Error = variant {
    // The user provided ckERC20 token is not supported by the minter.
    TokenNotSupported : record {supported_tokens : vec CkErc20Token};

```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L74-76)
```rust
    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,
```
