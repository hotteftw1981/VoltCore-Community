"""P04 automatic retry cooldown and manual override."""
import unittest
from datetime import datetime, timedelta, timezone
from app import ocpp_server


class P04RetryPolicy(unittest.TestCase):
    def test_recent_timeout_is_throttled(self):
        now=datetime(2026,10,9,10,0,tzinfo=timezone.utc)
        state={"status":"Timeout","last_attempt_at":(now-timedelta(seconds=30)).isoformat()}
        self.assertFalse(ocpp_server._automatic_local_list_retry_allowed(state,now=now))
        self.assertTrue(ocpp_server._automatic_local_list_retry_allowed(state,now=now+timedelta(seconds=100)))

    def test_other_statuses_and_unknown_times_are_eligible(self):
        now=datetime.now(timezone.utc)
        self.assertTrue(ocpp_server._automatic_local_list_retry_allowed({"status":"Ausstehend"},now=now))
        self.assertTrue(ocpp_server._automatic_local_list_retry_allowed({"status":"Fehler","last_attempt_at":None},now=now))

    def test_unverified_accepted_send_is_throttled(self):
        now=datetime(2026,10,9,10,0,tzinfo=timezone.utc)
        state={"status":"Prüfung erforderlich","last_attempt_at":(now-timedelta(seconds=15)).isoformat()}
        self.assertFalse(ocpp_server._automatic_local_list_retry_allowed(state,now=now))
        self.assertTrue(ocpp_server._automatic_local_list_retry_allowed(state,now=now+timedelta(seconds=125)))

    def test_manual_force_does_not_use_auto_throttle(self):
        import inspect
        source=inspect.getsource(ocpp_server.sync_pending_local_lists)
        self.assertIn("if not force_full and not _automatic_local_list_retry_allowed(state):",source)

if __name__=="__main__":
    unittest.main()
