import socket
import unittest
from unittest.mock import Mock,patch,sentinel
from urllib.error import URLError
import archiver as a


class FetcherDnsTests(unittest.TestCase):
    def test_negative_dns_tries_once_and_does_not_cache_future_recovery(self):
        fetcher=a.Fetcher(delay=0,retries=3)
        fetcher.opener=Mock()
        fetcher.opener.open.side_effect=[URLError(socket.gaierror(socket.EAI_NONAME,'Name or service not known')),sentinel.response]
        with patch.object(a.time,'sleep'):
            with self.assertRaises(URLError):fetcher.open('http://closed.test/trial.zip')
            self.assertEqual(fetcher.opener.open.call_count,1)
            self.assertIs(fetcher.open('http://closed.test/trial.zip'),sentinel.response)
        self.assertEqual(fetcher.opener.open.call_count,2)

    def test_temporary_resolution_error_still_retries(self):
        fetcher=a.Fetcher(delay=0,retries=3);fetcher.opener=Mock()
        fetcher.opener.open.side_effect=[URLError(socket.gaierror(socket.EAI_AGAIN,'Temporary failure')),sentinel.response]
        with patch.object(a.time,'sleep'):
            self.assertIs(fetcher.open('http://recovering.test/trial.zip'),sentinel.response)
        self.assertEqual(fetcher.opener.open.call_count,2)

    def test_timeout_keeps_configured_retry_count(self):
        fetcher=a.Fetcher(delay=0,retries=3);fetcher.opener=Mock()
        fetcher.opener.open.side_effect=URLError(TimeoutError('timed out'))
        with patch.object(a.time,'sleep'):
            with self.assertRaises(URLError):fetcher.open('http://slow.test/trial.zip')
        self.assertEqual(fetcher.opener.open.call_count,3)

    def test_untyped_error_message_is_not_assumed_to_be_negative_dns(self):
        fetcher=a.Fetcher(delay=0,retries=2);fetcher.opener=Mock()
        fetcher.opener.open.side_effect=URLError('Name or service not known')
        with patch.object(a.time,'sleep'):
            with self.assertRaises(URLError):fetcher.open('http://unknown.test/trial.zip')
        self.assertEqual(fetcher.opener.open.call_count,2)
