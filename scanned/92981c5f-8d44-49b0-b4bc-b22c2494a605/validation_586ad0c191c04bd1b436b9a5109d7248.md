The code is present and I can trace the exact logic. Let me analyze the claimed bug path precisely.

The bug is real and traceable to exact lines. Here is the full analysis:

---

### Title
Logo omission guard bypassed in `convert_proposal` when `return_self_describing_action=true` — (`rs/nns/governance/src/pb/proposal_conversions.rs`)

### Summary

`convert_proposal` derives `is_create_service_nervous_system_proposal` from the *converted* `action` field. When `return_self_describing_action=true`, `show_action` is forced to `false`, making `action` always `None`, so the flag is always `false`. As a result, `convert_self_describing_action` is called with `omit_logos=false` even when `omit_large_fields=true` was requested, and logo bytes are never suppressed in the `self_describing_action` path.

### Finding Description

**Step 1 — `for_list_proposals` sets `show_action = false`** [1](#0-0) 

When `return_self_describing_action=true`, `show_action = !true = false` and `show_self_describing_action = true`.

**Step 2 — `convert_proposal` sets `action = None`** [2](#0-1) 

Because `show_action()` is `false`, the branch returns `None`.

**Step 3 — `is_create_service_nervous_system_proposal` is always `false`** [3](#0-2) 

`action.as_ref().is_some_and(...)` on `None` is always `false`, regardless of the actual proposal type.

**Step 4 — `convert_self_describing_action` is called with `omit_logos=false`** [4](#0-3) 

The guard `is_create_service_nervous_system_proposal && display_options.omit_create_service_nervous_system_large_fields()` evaluates to `false && true = false`, so `paths_to_omit` is always empty: [5](#0-4) 

Logo and `token_logo` fields are passed through unmodified.

**Step 5 — The omit helper confirms the intent was to suppress logos** [6](#0-5) 

`omit_create_service_nervous_system_large_fields()` returns `true` for `list_proposals` with `omit_large_fields=true`, confirming the suppression was intended but is silently skipped.

**Step 6 — Existing test does not cover this combination**

The existing `test_omit_large_fields` test only checks the `action` field path (default `return_self_describing_action=false`): [7](#0-6) 

It never exercises `return_self_describing_action=true`, so the bug is untested.

### Impact Explanation

Any unprivileged caller can invoke `list_proposals` with `omit_large_fields=Some(true)` and `return_self_describing_action=Some(true)`. For every `CreateServiceNervousSystem` proposal that has a `self_describing_action` populated, the full logo blob (up to 256 KiB for `logo`, another 256 KiB for `ledger_parameters.token_logo`) is returned. The NNS has had dozens of SNS launches; if even ~5 proposals carry 512 KiB of logo data each, the aggregate response exceeds the IC 2 MiB query-response limit, causing serialization failure and a rejected query. The `omit_large_fields` invariant is violated unconditionally whenever `return_self_describing_action=true`.

### Likelihood Explanation

The trigger is a single query call with two boolean flags set to `true`. No privilege, no key, no social engineering is required. The NNS governance canister is publicly queryable by any principal. The combination of flags is a documented, intended API feature.

### Recommendation

In `convert_proposal`, derive `is_create_service_nervous_system_proposal` from the *original* `pb::Proposal`'s `action` field rather than from the already-converted (and potentially `None`) `action`:

```rust
let is_create_service_nervous_system_proposal = item.action.as_ref().is_some_and(|action| {
    matches!(action, pb::proposal::Action::CreateServiceNervousSystem(_))
});
```

This decouples the type-detection logic from the display-option-controlled conversion path.

### Proof of Concept

```rust
// State-machine test sketch
let governance = governance_with_proposals(vec![
    Action::CreateServiceNervousSystem(csns_with_256kib_logo()),
]);

let response = governance.list_proposals(
    &PrincipalId::new_anonymous(),
    ListProposalInfoRequest {
        omit_large_fields: Some(true),
        return_self_describing_action: Some(true),
        ..Default::default()
    },
);

// Bug: self_describing_action still contains the logo blob
let sda = response.proposal_info[0]
    .proposal.as_ref().unwrap()
    .self_describing_action.as_ref().unwrap();
// Assert logo field is Null — this assertion FAILS with the current code
assert_logo_is_null(sda);
```

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

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L59-61)
```rust
    pub fn omit_create_service_nervous_system_large_fields(&self) -> bool {
        self.omit_large_fields_requested && self.multi_query
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

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L495-499)
```rust
    let action = if display_options.show_action() {
        action.as_ref().map(|x| convert_action(x, display_options))
    } else {
        None
    };
```

**File:** rs/nns/governance/src/pb/proposal_conversions.rs (L500-502)
```rust
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

**File:** rs/nns/governance/src/governance/tests/list_proposals.rs (L615-628)
```rust
fn create_service_nervous_system_has_logo(proposal_info: &ProposalInfo) -> bool {
    let action = proposal_info
        .proposal
        .as_ref()
        .unwrap()
        .action
        .as_ref()
        .unwrap();
    if let ApiAction::CreateServiceNervousSystem(create_service_nervous_system) = action {
        create_service_nervous_system.logo.is_some()
    } else {
        panic!("Expected CreateServiceNervousSystem action")
    }
}
```
