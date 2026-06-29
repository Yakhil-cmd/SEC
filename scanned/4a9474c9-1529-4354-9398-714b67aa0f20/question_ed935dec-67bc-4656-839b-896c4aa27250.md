[File: 'engine-types/src/public_key.rs -> Scope: Critical. Insolvency'] [Function: FromStr / split_key_type_data / KeyType::from_str case-insensitive] Can an attacker exploit the case-insensitive parsing in KeyType::from_str (which calls to_ascii_lowercase before matching) to supply a public_key JSON string like 'ED25519:<bs58data>' or 'SECP256K1:<bs58data>', causing the key to be parsed as a valid PublicKey, but then when the engine logs or re-serializes the key via Display (which always outputs lowercase 'ed25519:' or 'secp256k1:'), the string representation differs from the input, so that if the input string is used as a lookup key in any map or comparison, the case-variant and

```python
questions = [
