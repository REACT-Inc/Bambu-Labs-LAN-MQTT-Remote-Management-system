import struct
import time
import unittest
from unittest.mock import MagicMock, patch
import camera_capture as camera


class Stream:
    def __init__(self, data, chunk=2):
        self.data=data;self.chunk=chunk;self.sent=None;self.closed=False
    def settimeout(self,value):assert value>0
    def recv(self,size):
        size=min(size,self.chunk);out=self.data[:size];self.data=self.data[size:];return out
    def sendall(self,data):self.sent=data
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.printer={'ip':'192.0.2.1','access_code':'12345678'}
        self.jpeg=b'\xff\xd8\xff\xe1test-image\xff\xd9'
    def packet(self,frame):return struct.pack('<IIII',len(frame),0,0,0)+frame
    def capture(self,stream):
        raw=Stream(b'');context=MagicMock();context.wrap_socket.return_value=stream
        with patch.object(camera.socket,'create_connection',return_value=raw),patch.object(camera.ssl,'SSLContext',return_value=context):
            result=camera.capture_once(self.printer,time.monotonic()+1)
        self.assertTrue(raw.closed);self.assertTrue(stream.closed)
        self.assertEqual(len(stream.sent),80)
        return result
    def test_fragmented_header_and_frame(self):
        self.assertEqual(self.capture(Stream(self.packet(self.jpeg),1)),self.jpeg)
    def test_coalesced_header_and_frame(self):
        self.assertEqual(self.capture(Stream(self.packet(self.jpeg),65536)),self.jpeg)
    def test_bad_frame_then_valid_frame(self):
        self.assertEqual(self.capture(Stream(self.packet(b'bad-frame')+self.packet(self.jpeg))),self.jpeg)
    def test_short_connection_raises(self):
        with self.assertRaises(ConnectionError):self.capture(Stream(b'123'))
    def test_oversized_frame_rejected(self):
        with self.assertRaises(ValueError):self.capture(Stream(struct.pack('<IIII',camera.MAX_FRAME+1,0,0,0)))
    def test_read_deadline(self):
        with self.assertRaises(TimeoutError):camera.read_exact(Stream(b'123'),3,time.monotonic()-1)
    def test_retry_then_success(self):
        with patch.object(camera,'capture_once',side_effect=[TimeoutError(),self.jpeg]) as attempt,patch.object(camera.time,'sleep'):
            self.assertEqual(camera.capture_jpeg(self.printer),self.jpeg)
            self.assertEqual(attempt.call_count,2)
    def test_two_failures_raise(self):
        with patch.object(camera,'capture_once',side_effect=ConnectionError()) as attempt,patch.object(camera.time,'sleep'):
            with self.assertRaises(ConnectionError):camera.capture_jpeg(self.printer)
            self.assertEqual(attempt.call_count,2)
