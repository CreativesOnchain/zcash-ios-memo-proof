//! Build an isolated, wire-valid fixture from Valar's public V6 sample.
use anyhow::{Context, Result, ensure};
use incrementalmerkletree::frontier::CommitmentTree;
use orchard::{
    Note,
    keys::{PreparedIncomingViewingKey, Scope},
    note::{ExtractedNoteCommitment, NoteVersion, RandomSeed},
    note_encryption::{IronwoodDomain, IronwoodNoteEncryption},
    tree::MerkleHashOrchard,
    value::NoteValue,
};
use serde_json::json;
use std::{fs, path::PathBuf};
use zcash_keys::keys::UnifiedSpendingKey;
use zcash_note_encryption::{Domain, try_note_decryption};
use zcash_primitives::{merkle_tree::write_commitment_tree, transaction::Transaction};
use zcash_protocol::consensus::{BranchId, Network};
use zip32::AccountId;

fn main() -> Result<()> {
    let template = PathBuf::from(
        std::env::args_os()
            .nth(1)
            .context("Pass the pinned Valar transaction fixture")?,
    );
    let mut raw = hex::decode(fs::read_to_string(template)?.trim())?;
    let tx = Transaction::read(&raw[..], BranchId::Nu6_3)?;
    let action = tx
        .ironwood_bundle()
        .context("Missing fixture Ironwood bundle")?
        .actions()
        .first();

    let ufvk = UnifiedSpendingKey::from_seed(&Network::MainNetwork, &[0x39; 32], AccountId::ZERO)
        .map_err(|_| anyhow::anyhow!("Fixture key derivation failed"))?
        .to_unified_full_viewing_key();
    let fvk = ufvk
        .orchard()
        .context("Fixture viewing key has no Orchard component")?;
    let scope = Scope::External;
    let rho = action.rho();
    let rseed = RandomSeed::from_bytes([0x37; 32], &rho).unwrap();
    let note = Note::from_parts(
        fvk.address_at(0u32, scope),
        NoteValue::from_raw(5_000),
        rho,
        rseed,
        NoteVersion::V3,
    )
    .unwrap();
    let mut memo = std::array::from_fn(|i| (i % 251) as u8);
    memo[0] = 0xff;
    let cmx = ExtractedNoteCommitment::from(note.commitment());
    let enc = IronwoodNoteEncryption::new(Some(fvk.to_ovk(scope)), note, memo);
    let out = enc.encrypt_outgoing_plaintext(action.cv_net(), &cmx, &mut rand::rng());

    let old = action.encrypted_note();
    let original = [
        action.cmx().to_bytes().as_slice(),
        &old.epk_bytes,
        old.enc_ciphertext.as_ref(),
        &old.out_ciphertext,
    ]
    .concat();
    let mut matches = raw
        .windows(original.len())
        .enumerate()
        .filter_map(|(index, bytes)| (bytes == original).then_some(index));
    let index = matches.next().context("Original action missing")?;
    ensure!(matches.next().is_none(), "Original action is ambiguous");
    let replacement = [
        cmx.to_bytes().as_slice(),
        &IronwoodDomain::epk_bytes(enc.epk()).0,
        enc.encrypt_note_plaintext().as_ref(),
        &out,
    ]
    .concat();
    raw[index..index + replacement.len()].copy_from_slice(&replacement);

    // The local chain confirms the wire encoding without verifying proofs or signatures.
    let tx = Transaction::read(&raw[..], BranchId::Nu6_3)?;
    let bundle = tx
        .ironwood_bundle()
        .context("Missing modified Ironwood bundle")?;
    let action = bundle.actions().first();
    let incoming = PreparedIncomingViewingKey::new(&fvk.to_ivk(scope));
    let (_, _, observed) =
        try_note_decryption(&IronwoodDomain::for_action(action), &incoming, action)
            .context("Viewing key failed to decrypt the serialized fixture")?;
    ensure!(observed == memo, "Serialized fixture memo mismatch");
    ensure!(
        tx.transparent_bundle().is_none()
            && tx.sapling_bundle().is_none()
            && tx.orchard_bundle().is_none(),
        "Unexpected secondary pool in fixture"
    );
    let mut tree = CommitmentTree::<MerkleHashOrchard, 32>::empty();
    let actions: Vec<_> = bundle
        .actions()
        .iter()
        .map(|action| {
            tree.append(MerkleHashOrchard::from_cmx(action.cmx()))
                .expect("Small fixture tree");
            let encrypted = action.encrypted_note();
            json!({
                "nullifier": hex::encode(action.nullifier().to_bytes()),
                "cmx": hex::encode(action.cmx().to_bytes()),
                "epk": hex::encode(encrypted.epk_bytes),
                "prefix": hex::encode(&encrypted.enc_ciphertext.as_ref()[..52]),
            })
        })
        .collect();
    let mut frontier = Vec::new();
    write_commitment_tree(&tree, &mut frontier)?;
    println!(
        "{}",
        json!({
            "txid": hex::encode(tx.txid().as_ref()),
            "tx": hex::encode(raw),
            "ufvk": ufvk.encode(&Network::MainNetwork).map_err(|_| anyhow::anyhow!("Fixture viewing key encoding failed"))?,
            "memo": hex::encode(memo),
            "ironwood_tree": hex::encode(frontier),
            "actions": actions,
        })
    );
    Ok(())
}
