//! mini_http — a tiny blocking HTTP client for loopback sidecar calls.
//!
//! The sidecar supervisor (see `sidecar.rs`) needs to make a handful of
//! HTTP calls against the Python backend it just spawned — health
//! probes, the graceful-shutdown POST, the pause/stop execution flags —
//! from a plain `std` supervisor thread, before (and independently of)
//! the WebView. Pulling in a full async HTTP stack for three loopback
//! endpoints would be wildly out of proportion, so this module speaks
//! just enough HTTP over `std::net::TcpStream`:
//!
//! * HTTP/1.0 request line + `Connection: close`, read-to-EOF response
//!   (a chunked transfer-encoding, if a server ever sends one despite
//!   the 1.0 request, is decoded defensively);
//! * 2 s connect/read/write timeouts — a hung backend must fail a probe
//!   fast, never wedge the supervisor;
//! * the launch token is sent BOTH as `Authorization: Bearer <token>`
//!   (the roadmap §4.5 contract) and as `X-Local-Token: <token>` — the
//!   header the shipped backend middleware actually checks
//!   (`backend/app/middleware.py`: `TOKEN_HEADER = "x-local-token"`).
//!   Bearer-only requests are rejected 401 by that middleware, so both
//!   headers are always sent together when a token is given.
//!
//! Only the literal loopback URLs the supervisor itself builds
//! (`http://127.0.0.1:<port>/…`) are expected here. This is not a
//! general-purpose client: no TLS, no redirects, no proxies.
//!
//! Return shape (all helpers): `Ok((status, body))` for ANY completed
//! HTTP exchange — including 4xx/5xx, which are answers, not transport
//! failures — and `Err(String)` only when no HTTP response was obtained
//! (connect refused, timeout, malformed status line, …). Callers decide
//! which statuses mean success for their endpoint.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

/// Timeout applied to connect, read and write alike.
const TIMEOUT: Duration = Duration::from_secs(2);

/// `GET <url>` — used for `/health/live` (no token) and `/health/ready`.
pub fn get(url: &str, token: Option<&str>) -> Result<(u16, String), String> {
    request("GET", url, token, None)
}

/// `POST <url>` with no body — used for `/api/v1/system/shutdown` (202).
pub fn post(url: &str, token: Option<&str>) -> Result<(u16, String), String> {
    request("POST", url, token, None)
}

/// `PUT <url>` with a body — used for `PUT /api/v1/settings`, the
/// backend's real surface for the persisted execution flags
/// (`execution.pause_all` / `execution.stop_all`, see
/// `backend/app/execution/flags.py` and `sidecar::set_execution_flag`).
pub fn put(
    url: &str,
    token: Option<&str>,
    content_type: &str,
    body: &[u8],
) -> Result<(u16, String), String> {
    request("PUT", url, token, Some((content_type, body)))
}

/// One blocking HTTP exchange. `url` is a full loopback URL
/// (`http://127.0.0.1:<port><path>`); `body` is `(content_type, bytes)`.
pub fn request(
    method: &str,
    url: &str,
    token: Option<&str>,
    body: Option<(&str, &[u8])>,
) -> Result<(u16, String), String> {
    let (authority, path) = parse_loopback_url(url)?;
    // `Host` must be exactly `127.0.0.1:<port>`: the backend middleware
    // allowlists loopback Host values (anti DNS-rebinding) and the
    // roadmap §4.5 pins the literal-IP form.
    let host_header = authority.clone();

    let mut addrs = authority
        .to_socket_addrs()
        .map_err(|e| format!("cannot resolve {authority}: {e}"))?;
    let addr = addrs
        .next()
        .ok_or_else(|| format!("cannot resolve {authority}: no address"))?;
    let mut stream = TcpStream::connect_timeout(&addr, TIMEOUT)
        .map_err(|e| format!("connect to {authority} failed: {e}"))?;
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let _ = stream.set_write_timeout(Some(TIMEOUT));
    let _ = stream.set_nodelay(true);

    let mut head = String::with_capacity(256);
    head.push_str(method);
    head.push(' ');
    head.push_str(&path);
    head.push_str(" HTTP/1.0\r\nHost: ");
    head.push_str(&host_header);
    head.push_str("\r\nConnection: close\r\n");
    if let Some(token) = token {
        // Both header spellings, always together — see the module docs:
        // the roadmap names Bearer; the shipped middleware checks
        // X-Local-Token.
        head.push_str("Authorization: Bearer ");
        head.push_str(token);
        head.push_str("\r\nX-Local-Token: ");
        head.push_str(token);
        head.push_str("\r\n");
    }
    let body_len = body.map(|(_, bytes)| bytes.len()).unwrap_or(0);
    if let Some((content_type, _)) = body {
        head.push_str("Content-Type: ");
        head.push_str(content_type);
        head.push_str("\r\n");
    }
    if body.is_some() || method == "POST" || method == "PUT" {
        head.push_str(&format!("Content-Length: {body_len}\r\n"));
    }
    head.push_str("\r\n");

    stream
        .write_all(head.as_bytes())
        .map_err(|e| format!("write to {authority} failed: {e}"))?;
    if let Some((_, bytes)) = body {
        stream
            .write_all(bytes)
            .map_err(|e| format!("write to {authority} failed: {e}"))?;
    }
    stream
        .flush()
        .map_err(|e| format!("flush to {authority} failed: {e}"))?;

    // `Connection: close` + HTTP/1.0 => the body ends at EOF. A read
    // timeout after some bytes arrived is treated as EOF (a server
    // holding the socket open past the timeout has already answered as
    // much as it is going to); a timeout with zero bytes is an error.
    let mut raw: Vec<u8> = Vec::with_capacity(1024);
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(e) => {
                if raw.is_empty() {
                    return Err(format!("read from {authority} failed: {e}"));
                }
                break;
            }
        }
    }

    parse_response(&raw)
}

/// Split `http://127.0.0.1:<port>/some/path` into (`authority`, `path`).
/// Only the `http://` loopback form the supervisor builds is accepted.
fn parse_loopback_url(url: &str) -> Result<(String, String), String> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| format!("unsupported URL (http:// loopback only): {url}"))?;
    match rest.find('/') {
        Some(idx) => Ok((rest[..idx].to_string(), rest[idx..].to_string())),
        None => Ok((rest.to_string(), "/".to_string())),
    }
}

/// Parse a raw HTTP response into `(status, body)`, decoding a chunked
/// transfer-encoding when one was used.
fn parse_response(raw: &[u8]) -> Result<(u16, String), String> {
    let split = find_subslice(raw, b"\r\n\r\n")
        .map(|i| (i, i + 4))
        .or_else(|| find_subslice(raw, b"\n\n").map(|i| (i, i + 2)))
        .ok_or_else(|| "malformed HTTP response: no header terminator".to_string())?;
    let head = String::from_utf8_lossy(&raw[..split.0]).into_owned();
    let mut lines = head.lines();
    let status_line = lines.next().unwrap_or("");
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| format!("malformed HTTP status line: {status_line:?}"))?;

    let chunked = head
        .to_ascii_lowercase()
        .lines()
        .any(|line| line.starts_with("transfer-encoding:") && line.contains("chunked"));

    let body_bytes = &raw[split.1..];
    let decoded = if chunked {
        decode_chunked(body_bytes)
    } else {
        body_bytes.to_vec()
    };
    Ok((status, String::from_utf8_lossy(&decoded).into_owned()))
}

/// Decode an HTTP chunked body; on any framing error, return the input
/// unchanged (best effort — the health/settings payloads this client
/// reads are small JSON objects, and the caller validates them anyway).
fn decode_chunked(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut rest = data;
    loop {
        let Some(eol) = find_subslice(rest, b"\r\n") else {
            return data.to_vec();
        };
        let size_text = String::from_utf8_lossy(&rest[..eol]).into_owned();
        let size_text = size_text.split(';').next().unwrap_or("").trim();
        let Ok(size) = usize::from_str_radix(size_text, 16) else {
            return data.to_vec();
        };
        let after_size = &rest[eol + 2..];
        if size == 0 {
            return out;
        }
        if after_size.len() < size {
            return data.to_vec();
        }
        out.extend_from_slice(&after_size[..size]);
        let after_chunk = &after_size[size..];
        rest = after_chunk.strip_prefix(b"\r\n").unwrap_or(after_chunk);
    }
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}
