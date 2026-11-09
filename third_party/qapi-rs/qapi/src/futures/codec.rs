use bytes::{BufMut, BytesMut};
use serde::{de::DeserializeOwned, Serialize};
use std::io;
use std::marker::PhantomData;

pub struct JsonLinesCodec<D = ()> {
    next_index: usize,
    max_length: Option<usize>,
    _decoder: PhantomData<fn() -> D>,
}

impl<D> JsonLinesCodec<D> {
    pub fn new() -> Self {
        Self {
            next_index: 0,
            max_length: None,
            _decoder: PhantomData,
        }
    }

    pub fn bounded(max_length: usize) -> Self {
        Self {
            max_length: Some(max_length),
            ..Self::new()
        }
    }

    fn check_length(&self, length: usize) -> io::Result<()> {
        if self.max_length.map_or(false, |limit| length > limit) {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "QMP frame exceeds limit",
            ))
        } else {
            Ok(())
        }
    }
}

impl<D: DeserializeOwned> JsonLinesCodec<D> {
    fn priv_decode(&mut self, buf: &mut BytesMut) -> Result<Option<D>, io::Error> {
        match memchr::memchr(b'\n', &buf[self.next_index..]) {
            Some(offset) => {
                let index = offset + self.next_index;
                self.check_length(index + 1)?;
                self.next_index = 0;
                let line = buf.split_to(index + 1);
                serde_json::from_slice(&line).map_err(From::from).map(Some)
            }
            None => {
                self.check_length(buf.len())?;
                self.next_index = buf.len();
                Ok(None)
            }
        }
    }

    fn priv_decode_eof(&mut self, buf: &mut BytesMut) -> Result<Option<D>, io::Error> {
        if let Some(value) = self.priv_decode(buf)? {
            return Ok(Some(value));
        }
        if buf.is_empty() {
            Ok(None)
        } else {
            self.next_index = 0;
            let line = buf.split_to(buf.len());
            serde_json::from_slice(&line).map_err(From::from).map(Some)
        }
    }
}

#[cfg(feature = "tokio-util")]
impl<D: DeserializeOwned> tokio_util::codec::Decoder for JsonLinesCodec<D> {
    type Item = D;
    type Error = io::Error;

    fn decode(&mut self, buf: &mut BytesMut) -> Result<Option<Self::Item>, Self::Error> {
        self.priv_decode(buf)
    }

    fn decode_eof(&mut self, buf: &mut BytesMut) -> Result<Option<Self::Item>, Self::Error> {
        self.priv_decode_eof(buf)
    }
}

#[cfg(feature = "tokio-util")]
impl<T, S: Serialize> tokio_util::codec::Encoder<S> for JsonLinesCodec<T> {
    type Error = io::Error;

    fn encode(&mut self, item: S, bytes: &mut BytesMut) -> Result<(), Self::Error> {
        let frame = serde_json::to_vec(&item)?;
        self.check_length(frame.len() + 1)?;
        bytes.extend_from_slice(&frame);
        bytes.put_u8(b'\n');
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use tokio_util::codec::Decoder;

    #[test]
    fn bound_is_per_frame_and_eof_consumes_a_suffix_once() {
        let mut codec = JsonLinesCodec::<Value>::bounded(8);
        let mut buffer = BytesMut::from(&b"{}\n{}\n{}\n{}"[..]);
        for _ in 0..3 {
            assert!(codec.decode(&mut buffer).unwrap().is_some());
        }
        assert!(codec.decode_eof(&mut buffer).unwrap().is_some());
        assert!(codec.decode_eof(&mut buffer).unwrap().is_none());
    }

    #[test]
    fn oversized_frame_and_malformed_eof_are_errors() {
        let mut codec = JsonLinesCodec::<Value>::bounded(8);
        assert!(codec
            .decode(&mut BytesMut::from(&b"{       }\n"[..]))
            .is_err());
        assert!(codec
            .decode_eof(&mut BytesMut::from(&b"{\"x\":"[..]))
            .is_err());
    }
}
