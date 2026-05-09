//! MemoryBackend trait for all storage backends.

use hope_core::{HopeError, RetrievalResult};
use serde_json::Value;

pub trait MemoryBackend: Send + Sync {
    fn backend_id(&self) -> &str;
    fn store(
        &self,
        content: &str,
        source: &str,
        metadata: Option<&Value>,
    ) -> Result<String, HopeError>;
    fn retrieve(
        &self,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<RetrievalResult>, HopeError>;
    fn delete(&self, doc_id: &str) -> Result<bool, HopeError>;
    fn clear(&self) -> Result<(), HopeError>;
    fn count(&self) -> Result<usize, HopeError>;
}
