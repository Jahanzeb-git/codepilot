use serde::Deserialize;

#[derive(Deserialize, Clone, Debug)]
pub struct ScopedKey {
    pub application_key_id: String,
    pub application_key: String,
    pub bucket_id: String,
    pub bucket_name: String,
    pub name_prefix: String,
    pub expires_at: i64, // unix seconds
}

pub async fn fetch_scoped_key(
    client: &reqwest::Client,
    endpoint: &str,
    machine_secret: &str,
) -> anyhow::Result<ScopedKey> {
    let resp = client
        .post(endpoint)
        .header("X-Machine-Secret", machine_secret)
        .send()
        .await?
        .error_for_status()?
        .json::<ScopedKey>()
        .await?;
    Ok(resp)
}
