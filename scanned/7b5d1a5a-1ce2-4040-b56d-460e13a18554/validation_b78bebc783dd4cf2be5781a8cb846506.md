The bug is real. Let me trace through the exact code path to confirm.

The bug is confirmed. Here is the complete trace:

---

### Title
Logo omission guard bypassed in `convert_proposal` when `return_self_describing_action=true` — (`rs/nns/governance/src/pb/proposal_conversions.rs`)

### Summary

`convert_proposal` derives `is_create_service_nervous_system_proposal` from the already-gated `action` variable. When `return_self_describing_action=true`, `show_action` is set to `false`, so `action` is forced to `None` before the check runs. The flag is therefore always `false`, and `convert_self_describing_action` is called with `omit_create_service_nervous_system_logos = false`, leaving the full logo bytes in the response regardless of `omit_large_fields=true`.

### Finding Description

**Step 1 — `ProposalDisplayOptions::for_list_proposals(true, true)`** [1](#0-0) 

When `return_self_describing_action=true`:
- `show_action = !true = false`
- `show_self_describing_action = true`
- `omit_large_fields_requested = true`

**Step 2 — `convert_proposal` gates `action` behind `show_action()`** [2](#0-1) 

Because `show_action()` is `false`, `action` is set to `None`. The very next line derives `is_create_service_nervous_system_proposal` from this already-`None` value, so it is always `false`.

**Step 3 — `convert_self_describing_action` receives `false` for the omit flag** [3](#0-2) 

`is_create_service_nervous_system_proposal && display_options.omit_create_service_nervous_system_large_fields()` evaluates to `false && true = false`. The logo-omission path inside `convert_self_describing_action` is never taken: [4](#0-3) 

`paths_to_omit` is an empty `vec![]`, so the full logo bytes are serialised into the response.

**Root cause in one sentence:** `is_create_service_nervous_system_proposal` is computed from the *output* of the `show_action` gate rather than from the raw `pb::Proposal` action, so the two branches (`action` and `self_describing_action`) are not coordinated.

### Impact Explanation

The `omit_large_fields` field was introduced specifically to keep `list_proposals` responses under the IC 2 MB query-response limit: [5](#0-4) 

When a caller passes `omit_large_fields=Some(true)` together with `return_self_describing_action=Some(true)`, the logo (potentially hundreds of KiB of base64 text) is still included in `self_describing_action.value`. With enough such proposals in a single `list_proposals` page the serialised response can exceed 2 MB, causing the IC runtime to reject the response and the query to fail with a system-level error. The canister itself is not permanently harmed, but the query endpoint becomes unreachable for any client that uses both flags simultaneously.

### Likelihood Explanation

- No privilege is required; `list_proposals` is a public query callable by any anonymous principal.
- `CreateServiceNervousSystem` proposals with non-trivial logos exist on mainnet.
- The `return_self_describing_action` flag is the intended path for newer NNS frontends and tooling.
- The combination of both flags is exactly the documented "safe" way to list proposals without hitting the size limit, so clients that follow the documentation are the ones most likely to hit the bug.

### Recommendation

Derive `is_create_service_nervous_system_proposal` from the raw `pb::Proposal` action *before* the `show_action` gate, for example:

```rust
// In convert_proposal, before the show_action gate:
let is_create_service_nervous_system_proposal = matches!(
    action,  // the raw pb::proposal::Action Option, not the converted one
    Some(pb::proposal::Action::CreateServiceNervousSystem(_))
);
```

This decouples the type-detection logic from the display-gating logic so both branches honour `omit_large_fields` independently.

### Proof of Concept

State-machine test sketch (mirrors the existing `test_omit_large_fields` pattern):

```rust
// governance_with_proposals creates a CSNS proposal with a 256 KiB logo
let governance = governance_with_proposals(vec![Action::CreateServiceNervousSystem(
    large_logo_csns(),
)]);

let response = governance.list_proposals(
    &PrincipalId::new_anonymous(),
    ListProposalInfoRequest {
        omit_large_fields: Some(true),
        return_self_describing_action: Some(true),
        ..Default::default()
    },
);

// BUG: self_describing_action still contains the logo
let sda = response.proposal_info[0]
    .proposal.as_ref().unwrap()
    .self_describing_action.as_ref().unwrap();
// assert logo field is Null — this assertion FAILS today
assert_logo_is_null(sda);
```

The existing unit test `test_omit_large_fields` only exercises `omit_large_fields=true` without `return_self_describing_action=true`, so it does not catch this regression. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L17-27)
```rust
    pub fn for_list_proposals(
        omit_large_fields_requested: bool,
        return_self_describing_action: bool,
    ) -> Self {
        Self {
            omit_large_fields_requested,
            show_self_describing_action: return_self_describing_action,
            show_action: !return_self_describing_action,
            multi_query: true,
        }
    }
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L317-328)
```rust
    let paths_to_omit = if omit_create_service_nervous_system_logos {
        vec![
            RecordPath {
                fields_names: vec!["logo"],
            },
            RecordPath {
                fields_names: vec!["ledger_parameters", "token_logo"],
            },
        ]
    } else {
        vec![]
    };
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L495-502)
```rust
    let action = if display_options.show_action() {
        action.as_ref().map(|x| convert_action(x, display_options))
    } else {
        None
    };
    let is_create_service_nervous_system_proposal = action.as_ref().is_some_and(|action| {
        matches!(action, api::proposal::Action::CreateServiceNervousSystem(_))
    });
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L503-515)
```rust
    let self_describing_action = if display_options.show_self_describing_action() {
        self_describing_action
            .as_ref()
            .map(|self_describing_action| {
                convert_self_describing_action(
                    self_describing_action,
                    is_create_service_nervous_system_proposal
                        && display_options.omit_create_service_nervous_system_large_fields(),
                )
            })
    } else {
        None
    };
```

**File:** rs/nns/governance/api/src/types.rs (L3226-3230)
```rust
    /// Omits "large fields" from the response. Currently only omits the
    /// `logo` and `token_logo` field of CreateServiceNervousSystem proposals. This
    /// is useful to improve download times and to ensure that the response to the
    /// request doesn't exceed the message size limit.
    pub omit_large_fields: Option<bool>,
```

**File:** rs/nns/governance/src/governance/tests/list_proposals.rs (L630-670)
```rust
#[test]
fn test_omit_large_fields() {
    let governance = governance_with_proposals(vec![Action::CreateServiceNervousSystem(
        CREATE_SERVICE_NERVOUS_SYSTEM.clone(),
    )]);

    let response = governance.list_proposals(
        &PrincipalId::new_anonymous(),
        ListProposalInfoRequest {
            omit_large_fields: Some(false),
            ..ListProposalInfoRequest::default()
        },
    );
    assert!(
        create_service_nervous_system_has_logo(&response.proposal_info[0]),
        "{response:?}"
    );

    let response = governance.list_proposals(
        &PrincipalId::new_anonymous(),
        ListProposalInfoRequest {
            omit_large_fields: None,
            ..ListProposalInfoRequest::default()
        },
    );
    assert!(
        create_service_nervous_system_has_logo(&response.proposal_info[0]),
        "{response:?}"
    );

    let response = governance.list_proposals(
        &PrincipalId::new_anonymous(),
        ListProposalInfoRequest {
            omit_large_fields: Some(true),
            ..ListProposalInfoRequest::default()
        },
    );
    assert!(
        !create_service_nervous_system_has_logo(&response.proposal_info[0]),
        "{response:?}"
    );
```
