import os

from api_test_case import ApiTestCase

from services import postApi


class ApiHarnessTests(ApiTestCase):

    def test_request_without_a_key_is_refused(self):
        response = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
        )

        # Starlette returns 401 on current versions and 403 on older ones;
        # either way HTTPBearer refuses the request.
        self.assertIn(response.status_code, (401, 403))
        self.assertNotIn("stat", response.json())

    def test_api_reads_keys_from_the_temporary_config_directory(self):
        raw_key = postApi.generate_new_api_key()
        self.assertTrue(
            os.path.exists(os.path.join(self.config_dir, "keys.json"))
        )

        response = self.client.post(
            "/sms/",
            json={"number": "+14155552671", "text": "hello"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("stat", response.json())
        self.assertEqual(postApi.get_config_dir(), self.config_dir)
