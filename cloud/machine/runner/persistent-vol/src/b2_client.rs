use crate::credentials::ScopedKey;
use serde::Deserialize;
use sha1::{Sha1, Digest as Sha1Digest};

#[derive(Clone)]
pub struct B2Session {
    pub api_url: String,
    pub download_url: String,
    pub auth_token: String,
    pub bucket_id: String,
    pub bucket_name: String,
    pub name_prefix: String,
    pub expires_at: i64,
}

pub async fn authorize(client: &reqwest::Client, key: &ScopedKey) -> anyhow::Result<B2Session> {
    let resp: serde_json::Value = client
        .get("https://api.backblazeb2.com/b2api/v3/b2_authorize_account")
        .basic_auth(&key.application_key_id, Some(&key.application_key))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;

    let api_info = &resp["apiInfo"]["storageApi"];
    Ok(B2Session {
        api_url: api_info["apiUrl"].as_str().unwrap().to_string(),
        download_url: api_info["downloadUrl"].as_str().unwrap().to_string(),
        auth_token: resp["authorizationToken"].as_str().unwrap().to_string(),
        bucket_id: key.bucket_id.clone(),
        bucket_name: key.bucket_name.clone(),
        name_prefix: key.name_prefix.clone(),
        expires_at: key.expires_at,
    })
}

#[derive(Deserialize, Clone)]
pub struct UploadUrl {
    #[serde(rename = "uploadUrl")]
    pub upload_url: String,
    #[serde(rename = "authorizationToken")]
    pub auth_token: String,
}

pub async fn get_upload_url(client: &reqwest::Client, session: &B2Session) -> anyhow::Result<UploadUrl> {
    let resp = client
        .post(format!("{}/b2api/v3/b2_get_upload_url", session.api_url))
        .header("Authorization", &session.auth_token)
        .json(&serde_json::json!({ "bucketId": session.bucket_id }))
        .send()
        .await?
        .error_for_status()?
        .json::<UploadUrl>()
        .await?;
    Ok(resp)
}

pub async fn upload_file(
    client: &reqwest::Client,
    upload_url: &UploadUrl,
    key: &str,
    bytes: Vec<u8>,
) -> anyhow::Result<()> {
    // B2 requires SHA1 of the body for integrity on this endpoint (unrelated
    // to the SHA256 we use in the manifest for change-detection).
    let mut hasher = Sha1::new();
    hasher.update(&bytes);
    let sha1_hex = hex::encode(hasher.finalize());

    let resp = client
        .post(&upload_url.upload_url)
        .header("Authorization", &upload_url.auth_token)
        .header("X-Bz-File-Name", urlencoding::encode(key).as_ref())
        .header("Content-Type", "b2/x-auto")
        .header("X-Bz-Content-Sha1", sha1_hex)
        .header("Content-Length", bytes.len().to_string())
        .body(bytes)
        .send()
        .await?;

    if !resp.status().is_success() {
        anyhow::bail!(
            "upload failed for {key}: {} - {}",
            resp.status(),
            resp.text().await.unwrap_or_default()
        );
    }
    Ok(())
}

pub async fn download_file(
    client: &reqwest::Client,
    session: &B2Session,
    key: &str,
) -> anyhow::Result<Vec<u8>> {
    let url = format!(
        "{}/file/{}/{}",
        session.download_url,
        session.bucket_name,
        urlencoding::encode(key)
    );
    let resp = client
        .get(&url)
        .header("Authorization", &session.auth_token)
        .send()
        .await?
        .error_for_status()?;
    Ok(resp.bytes().await?.to_vec())
}