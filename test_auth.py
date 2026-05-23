import unittest
from app import app, socketio, app_state, authenticated_mobiles, state_lock

class AuthTestCase(unittest.TestCase):
    def setUp(self):
        # We need to test the socket logic
        app.testing = True
        self.client = socketio.test_client(app)

    def test_bad_pin_fails(self):
        # Join as mobile
        self.client.emit('join', {'client_type': 'mobile'})
        
        # Emit a bad PIN
        self.client.emit('pin_auth', {'pin': '000000'})
        
        # Get received events
        received = self.client.get_received()
        
        # We should have received a 'pin_auth_status' with success: False
        auth_status_events = [ev for ev in received if ev['name'] == 'pin_auth_status']
        self.assertTrue(len(auth_status_events) > 0)
        self.assertFalse(auth_status_events[-1]['args'][0]['success'])
        
        # Verify not in authenticated_mobiles
        # In test_client, the sid might be different, but we can just check it's empty
        # if no one successfully authenticated
        with state_lock:
            self.assertEqual(len(authenticated_mobiles), 0)

if __name__ == '__main__':
    unittest.main()
