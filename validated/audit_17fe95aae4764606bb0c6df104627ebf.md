### Title
Missing Slippage Protection in `TreasuryManager` `DepositRequest` API Enables Value Extraction from SNS Treasury DEX Deposits — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/treasury_manager/src/lib.rs`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The IC's `TreasuryManager` extension framework, used by SNS DAOs to deposit treasury tokens into DEX liquidity pools (e.g., via the KongSwap adaptor), defines a `DepositRequest` type that contains only `allowances` (amounts to deposit) and no slippage protection parameters. The IC governance code that constructs and dispatches deposit payloads likewise passes no minimum LP token or minimum price ratio constraints. An attacker who monitors the publicly readable SNS governance canister state can observe an approved deposit proposal and manipulate the DEX pool price before the deposit is executed, causing the SNS treasury to receive fewer LP tokens than expected. The IC developers themselves acknowledge this risk in both the API specification and the proposal rendering code.

---

### Finding Description

The `TreasuryManager` API is defined in two production IC files:

**`rs/sns/treasury_manager/treasury_manager.did`** (lines 35–40 and 84–86):
```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
...
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**`rs/sns/treasury_manager/src/lib.rs`** (lines 284–287):
```rust
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

The `DepositRequest` carries only the token amounts to deposit. There is no `min_lp_tokens_out`, no `min_price_ratio`, and no deadline field. Any `TreasuryManager` implementation that follows this interface — including the production KongSwap adaptor (`kongswap-adaptor-canister.wasm.gz`, referenced in `MODULE.bazel` lines 554–558) — cannot enforce slippage protection at the protocol level because the API provides no mechanism to express it.

The IC governance code that constructs the deposit payload is in **`rs/sns/governance/src/extensions.rs`** (`construct_treasury_manager_deposit_payload`, lines 1088–1099):
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;
    let arg = DepositRequest { allowances };
    ...
}
```

No slippage parameters are constructed or passed. The same pattern applies to the init payload for `RegisterExtension` (`construct_treasury_manager_init_payload`, lines 1071–1079).

The proposal rendering code in **`rs/sns/governance/src/proposal.rs`** (`validate_and_render_register_extension`, lines 1540–1545) explicitly warns voters about this:
```
## WARNING
Some Decentralized Exchanges lack slippage protection during deposits. Consequently,
deposited asset ratios may deviate from those specified in the proposal.
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

The warning is informational only — no enforcement or mitigation is applied at the protocol level.

---

### Impact Explanation

When an SNS DAO approves a `RegisterExtension` or `ExecuteExtensionOperation` (deposit) proposal, the governance canister calls the extension canister's `deposit` method, which in turn calls the DEX canister to add liquidity. These are separate asynchronous inter-canister message executions. Because the `DepositRequest` carries no slippage bound, the DEX deposit will succeed at whatever price ratio exists at execution time, regardless of how far it has moved from the ratio at proposal approval time.

An attacker who successfully manipulates the pool price before the deposit lands causes the SNS treasury to receive fewer LP tokens than it should for the deposited token amounts. The attacker then reverses their position and profits. The IC developers' own note ("any undeposited tokens are automatically returned") only partially mitigates this: the returned excess of one token does not compensate for the LP tokens lost due to the skewed ratio at which the other token was deposited.

---

### Likelihood Explanation

SNS governance proposals are publicly readable via query calls to the governance canister at any time. A proposal's adoption is visible before execution. On the IC, inter-canister calls are asynchronous: the governance canister calls the extension canister, which calls the DEX canister, across separate message-execution rounds. An attacker can submit a DEX manipulation transaction in the window between proposal adoption and the DEX call landing. The KongSwap adaptor is already deployed on mainnet (canister `2ipq2-uqaaa-aaaar-qailq-cai`, referenced in the integration test at `rs/nervous_system/integration_tests/tests/sns_extension_test.rs` line 963), making this a live production risk for any SNS that registers the extension.

---

### Recommendation

1. **Extend `DepositRequest`** in `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs` to include optional slippage parameters (e.g., `min_lp_tokens_out : opt nat`, `max_price_deviation_bps : opt nat64`).
2. **Propagate slippage parameters** through `construct_treasury_manager_deposit_payload` and `construct_treasury_manager_init_payload` in `rs/sns/governance/src/extensions.rs`, sourcing them from the proposal arguments.
3. **Require TreasuryManager implementations** to enforce the slippage bound when calling the DEX, rejecting the deposit if the bound is violated.
4. **Add a deadline parameter** to prevent stale proposals from executing at arbitrarily future prices.

---

### Proof of Concept

1. An SNS DAO submits and adopts a `RegisterExtension` proposal for the KongSwap adaptor with `treasury_allocation_sns_e8s = 200 * E8` and `treasury_allocation_icp_e8s = 100 * E8`.
2. An attacker observes the adopted proposal via a query call to the SNS governance canister.
3. Before the governance canister executes the proposal (calls the extension canister's `deposit`), the attacker submits a large buy of the SNS token on KongSwap, skewing the SNS/ICP ratio significantly.
4. The governance canister executes the proposal: `construct_treasury_manager_deposit_payload` builds a `DepositRequest { allowances: [...] }` with no slippage bound and calls the extension canister.
5. The extension canister calls KongSwap's `add_liquidity` at the manipulated price. The SNS treasury receives far fewer LP tokens than it would at the fair price.
6. The attacker sells their SNS tokens back, profiting from the artificially inflated price.
7. The IC code path: `ValidatedRegisterExtension::execute` → `approve_treasury_manager` → `upgrade_non_root_canister` (installs extension with `DepositRequest` containing only `allowances`) → extension canister's periodic deposit task → KongSwap `add_liquidity` with no minimum LP token check. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/governance/src/extensions.rs (L505-555)
```rust
impl ValidatedRegisterExtension {
    pub async fn execute(self, governance: &Governance) -> Result<(), GovernanceError> {
        let main = async || {
            let context = governance.treasury_manager_deposit_context().await?;

            let ValidatedRegisterExtension {
                spec,
                init,
                extension_canister_id,
                wasm,
            } = self;

            governance
                .register_extension_with_root(extension_canister_id)
                .await?;

            // Before granting any SNS capabilities to the extension, we must ensure that old code
            // could not have snuck in between proposal (re-)validation and the SNS assuming control.
            governance
                .ensure_no_code_is_installed(extension_canister_id)
                .await?;

            // This needs to happen before the canister code is installed.
            let init_blob = match init {
                ValidatedExtensionInit::TreasuryManager(ValidatedDepositOperationArg {
                    treasury_allocation_sns_e8s,
                    treasury_allocation_icp_e8s,
                    original,
                }) => {
                    let init_blob =
                        construct_treasury_manager_init_payload(context.clone(), original)
                            .map_err(|err| {
                                GovernanceError::new_with_message(
                                    ErrorType::InvalidProposal,
                                    format!(
                                        "Error constructing TreasuryManagerInit payload: {err}"
                                    ),
                                )
                            })?;

                    governance
                        .approve_treasury_manager(
                            extension_canister_id,
                            treasury_allocation_sns_e8s,
                            treasury_allocation_icp_e8s,
                        )
                        .await?;

                    init_blob
                }
            };
```

**File:** rs/sns/governance/src/extensions.rs (L1071-1079)
```rust
fn construct_treasury_manager_init_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = TreasuryManagerArg::Init(TreasuryManagerInit { allowances });
    candid::encode_one(&arg).map_err(|err| format!("Error encoding TreasuryManagerArg: {err}"))
}
```

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```

**File:** MODULE.bazel (L552-558)
```text
# SNS-KongSwap Adaptor canister (an SNS extension of the TreasuryManager kind)

http_file(
    name = "kongswap-adaptor-canister",
    downloaded_file_path = "kongswap-adaptor-canister.wasm.gz",
    sha256 = "1c07ceba560e7bcffa43d1b5ae97db81151854f068b707c1728e213948212a6c",
    url = "https://github.com/dfinity/sns-kongswap-adaptor/releases/download/v1.0.0/kongswap-adaptor-canister.wasm.gz",
```
