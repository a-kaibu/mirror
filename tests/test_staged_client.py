import unittest
from unittest.mock import Mock,patch
import single_fetch as transfer
import transfer_probe


class StagedClientTimeoutTests(unittest.TestCase):
    def test_large_control_operations_use_a_longer_bounded_read_timeout(self):
        for size,kwargs in [(None,{}),(2*1024**3,{}),(2*1024**3+1,{'read_timeout':600}),(4*1024**3,{'read_timeout':600})]:
            with self.subTest(size=size),patch.object(transfer,'make_client') as make:
                self.assertIs(transfer.staging_client('hf',size),make.return_value)
                make.assert_called_once_with('hf',**kwargs)

    def test_s3_configuration_keeps_the_requested_timeout_and_retry_policy(self):
        for timeout in (60,600):
            with self.subTest(timeout=timeout),patch('boto3.Session') as session:
                transfer_probe.client('hf',read_timeout=timeout)
                config=session.return_value.client.call_args.kwargs['config']
                self.assertEqual(config.read_timeout,timeout);self.assertEqual(config.connect_timeout,10)
                self.assertEqual(config.retries,{'total_max_attempts':2})

    def test_invalid_control_timeouts_cannot_construct_a_client(self):
        for timeout in (0,601,True,1.5):
            with self.subTest(timeout=timeout),patch('boto3.Session') as session:
                with self.assertRaises(ValueError):transfer_probe.client('hf',read_timeout=timeout)
                session.assert_not_called()
