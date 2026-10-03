"""Real Chromium fake-camera checks; exceptional hardware states are simulated."""
import functools
import http.server
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import zlib

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CameraBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(QuietHandler, directory=str(ROOT))
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = os.environ.get('CAMERA_TEST_URL', f'http://127.0.0.1:{cls.server.server_port}/')
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True,
            args=['--use-fake-device-for-media-stream=device-count=2', '--use-fake-ui-for-media-stream'],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(permissions=['camera'], accept_downloads=True)
        self.context.add_init_script("""
          window.testStreams = [];
          window.testConstraints = [];
          window.revokedUrls = [];
          if (navigator.mediaDevices) {
            const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            navigator.mediaDevices.getUserMedia = async constraints => {
              testConstraints.push(constraints);
              const stream = await original(constraints);
              testStreams.push(stream);
              return stream;
            };
          }
          const revoke = URL.revokeObjectURL.bind(URL);
          URL.revokeObjectURL = url => { revokedUrls.push(url); revoke(url); };
        """)
        self.page = self.context.new_page()
        self.errors = []
        self.requests = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda msg: self.errors.append(msg.text) if msg.type == 'error' else None)
        self.page.on('request', lambda request: self.requests.append((request.method, request.url)))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], 'Unexpected JavaScript/CSP errors')

    def open(self):
        response = self.page.goto(self.url)
        self.assertEqual(response.status, 200)
        expect(self.page.locator('#status')).not_to_have_text('カメラを確認しています…')

    def start(self):
        self.page.locator('#start-button').click()
        expect(self.page.locator('#capture-button')).to_be_enabled()
        expect(self.page.locator('#status')).to_contain_text('カメラを開始しました')

    def poll(self, expression, arg=None):
        # wait_for_function uses eval internally, which the app's CSP prohibits.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.page.evaluate(expression, arg):
                return
            self.page.wait_for_timeout(40)
        self.fail(f'Timed out: {expression}')

    def capture(self):
        previous = self.page.locator('#download-link').get_attribute('href')
        self.page.locator('#capture-button').click()
        self.poll("previous => {const link = document.querySelector('#download-link'); return link.hasAttribute('href') && link.getAttribute('href') !== previous;}", previous)
        expect(self.page.locator('#download-link')).to_be_visible()
        self.poll("() => {const img = document.querySelector('#photo'); return img.complete && img.naturalWidth > 0;}")

    def assert_png(self, data, expected_size):
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', data[16:24]), expected_size)
        offset = 8
        image_data = b''
        kinds = []
        while offset < len(data):
            length = struct.unpack('>I', data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            crc = struct.unpack('>I', data[offset + 8 + length:offset + 12 + length])[0]
            self.assertEqual(zlib.crc32(kind + payload) & 0xffffffff, crc)
            kinds.append(kind)
            if kind == b'IDAT':
                image_data += payload
            offset += length + 12
        self.assertEqual(kinds[-1], b'IEND')
        self.assertGreater(len(zlib.decompress(image_data)), 100)

    def test_initial_load_does_not_activate_camera(self):
        self.open()
        expect(self.page.locator('#camera-select option')).to_have_count(2)
        self.assertEqual(self.page.evaluate('testConstraints.length'), 0)
        expect(self.page.locator('#capture-button')).to_be_disabled()
        expect(self.page.locator('#stop-button')).to_be_disabled()
        expect(self.page.locator('#download-link')).to_be_hidden()

    def test_capture_download_stop_restart_and_privacy(self):
        self.open()
        self.start()
        self.assertEqual(self.page.evaluate('testStreams[0].getAudioTracks().length'), 0)
        size = tuple(self.page.evaluate("() => [document.querySelector('#video').videoWidth, document.querySelector('#video').videoHeight]"))
        self.capture()
        old_url = self.page.locator('#download-link').get_attribute('href')
        self.assertTrue(old_url.startswith('blob:'))
        self.page.locator('#stop-button').click()
        expect(self.page.locator('#capture-button')).to_be_disabled()
        expect(self.page.locator('#photo')).to_be_visible()
        self.assertEqual(self.page.evaluate("testStreams[0].getVideoTracks()[0].readyState"), 'ended')
        with self.page.expect_download() as event:
            self.page.locator('#download-link').click()
        download = event.value
        self.assertRegex(download.suggested_filename, r'^migaku-camera-[0-9-]+\.png$')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / download.suggested_filename
            download.save_as(path)
            self.assert_png(path.read_bytes(), size)
        self.start()
        self.capture()
        self.assertIn(old_url, self.page.evaluate('revokedUrls'))
        self.page.locator('#clear-button').click()
        expect(self.page.locator('#photo')).to_be_hidden()
        expect(self.page.locator('#download-link')).to_be_hidden()
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 2)
        # Only HTML, JS, CSS GET requests occur. Media and photo stay local.
        host = urlsplit(self.url).netloc
        self.assertTrue(self.requests)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests), self.requests)
        self.assertTrue(all(urlsplit(url).scheme in ('http', 'https', 'blob') for _, url in self.requests), self.requests)
        network = [(method, url) for method, url in self.requests if urlsplit(url).scheme != 'blob']
        self.assertTrue(all(urlsplit(url).netloc == host for _, url in network), network)
        self.assertTrue(all(urlsplit(url).path.endswith(('/', '/app.js', '/style.css', '/index.html')) for _, url in network), network)
        self.assertTrue(all(url.startswith('blob:' + self.url.rstrip('/')) for _, url in self.requests if urlsplit(url).scheme == 'blob'), self.requests)

    def test_camera_switch_and_resolutions(self):
        self.open()
        self.start()
        first_id = self.page.locator('#camera-select').input_value()
        second_id = self.page.locator('#camera-select option').nth(1).get_attribute('value')
        self.assertNotEqual(first_id, second_id)
        self.page.locator('#camera-select').select_option(second_id)
        self.poll('testStreams.length === 2')
        expect(self.page.locator('#capture-button')).to_be_enabled()
        self.assertEqual(self.page.evaluate('testStreams[1].getVideoTracks()[0].getSettings().deviceId'), second_id)
        self.assertEqual(self.page.evaluate('testStreams[0].getVideoTracks()[0].readyState'), 'ended')
        for value, width, height in [('qvga', 320, 240), ('vga', 640, 480), ('hd', 1280, 720), ('fullhd', 1920, 1080)]:
            self.page.locator('#resolution-select').select_option(value)
            expect(self.page.locator('#video-size')).to_have_text(f'{width} × {height} px')
            expect(self.page.locator('#capture-button')).to_be_enabled()
            self.capture()
            self.assertEqual(self.page.evaluate("() => [document.querySelector('#photo').naturalWidth, document.querySelector('#photo').naturalHeight]"), [width, height])
        self.page.locator('#resolution-select').select_option('default')
        expect(self.page.locator('#capture-button')).to_be_enabled()
        self.assertTrue(self.page.evaluate("testStreams.slice(0, -1).every(s => s.getVideoTracks()[0].readyState === 'ended')"))

    def test_missing_camera_and_reconnect(self):
        self.page.add_init_script("""
          window.nativeEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
          navigator.mediaDevices.enumerateDevices = async () => [];
          navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('', 'NotFoundError'); };
        """)
        self.open()
        expect(self.page.locator('#status')).to_contain_text('カメラが見つかりません')
        expect(self.page.locator('#camera-select')).to_be_disabled()
        self.page.locator('#start-button').click()
        expect(self.page.locator('#status')).to_contain_text('USBカメラの接続を確認')
        self.page.evaluate('navigator.mediaDevices.enumerateDevices = nativeEnumerate')
        self.page.locator('#refresh-button').click()
        expect(self.page.locator('#camera-select option')).to_have_count(2)
        expect(self.page.locator('#status')).to_contain_text('カメラを検出しました')

    def test_permission_busy_and_unsupported_resolution_guidance(self):
        self.open()
        cases = [('NotAllowedError', '許可されていません'), ('NotReadableError', '使用中'), ('OverconstrainedError', 'カメラの標準')]
        for name, message in cases:
            with self.subTest(error=name):
                self.page.evaluate("name => { navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('', name); }; }", name)
                self.page.locator('#start-button').click()
                expect(self.page.locator('#status')).to_contain_text(message)
                expect(self.page.locator('#start-button')).to_be_enabled()
                expect(self.page.locator('#capture-button')).to_be_disabled()
                expect(self.page.locator('#stop-button')).to_be_disabled()

    def test_pending_permission_cancel_releases_late_stream(self):
        self.page.add_init_script("""
          const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async constraints => {
            const stream = await original(constraints);
            return new Promise(resolve => { window.releasePending = () => resolve(stream); });
          };
        """)
        self.open()
        self.page.locator('#start-button').click()
        expect(self.page.locator('#stop-button')).to_be_enabled()
        expect(self.page.locator('#camera-select')).to_be_disabled()
        self.poll("typeof releasePending === 'function'")
        self.page.locator('#stop-button').click()
        expect(self.page.locator('#start-button')).to_be_enabled()
        self.page.evaluate('releasePending()')
        self.poll("testStreams[0].getVideoTracks()[0].readyState === 'ended'")
        self.assertIsNone(self.page.evaluate("document.querySelector('#video').srcObject"))
        expect(self.page.locator('#status')).to_contain_text('停止しました')

    def test_camera_disconnect_and_page_cleanup(self):
        self.open()
        self.start()
        self.page.evaluate("testStreams[0].getVideoTracks()[0].dispatchEvent(new Event('ended'))")
        expect(self.page.locator('#status')).to_contain_text('接続が終了しました')
        expect(self.page.locator('#capture-button')).to_be_disabled()
        self.assertEqual(self.page.evaluate("testStreams[0].getVideoTracks()[0].readyState"), 'ended')
        self.start()
        self.capture()
        self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        self.assertTrue(self.page.evaluate("testStreams.every(s => s.getVideoTracks()[0].readyState === 'ended')"))
        expect(self.page.locator('#download-link')).to_be_hidden()
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 1)

    def test_unsupported_browser(self):
        self.page.add_init_script("Object.defineProperty(navigator, 'mediaDevices', {value: undefined})")
        self.open()
        expect(self.page.locator('#status')).to_contain_text('対応していません')
        expect(self.page.locator('#start-button')).to_be_disabled()
        expect(self.page.locator('#refresh-button')).to_be_disabled()

    def test_insecure_http_is_rejected(self):
        def serve_local(route):
            name = urlsplit(route.request.url).path.rsplit('/', 1)[-1] or 'index.html'
            content_type = {'index.html': 'text/html', 'app.js': 'application/javascript', 'style.css': 'text/css'}[name]
            route.fulfill(body=(ROOT / name).read_bytes(), content_type=content_type)
        self.page.route('http://camera.test/**', serve_local)
        self.page.goto('http://camera.test/')
        expect(self.page.locator('#status')).to_contain_text('HTTPS')
        expect(self.page.locator('#start-button')).to_be_disabled()

    def test_real_browser_permission_denial(self):
        # Without fake UI or a permission grant, headless Chromium denies access.
        browser = self.playwright.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--use-fake-device-for-media-stream'],
        )
        try:
            page = browser.new_page()
            page.goto(self.url)
            page.locator('#start-button').click()
            expect(page.locator('#status')).to_contain_text('許可されていません')
            expect(page.locator('#capture-button')).to_be_disabled()
        finally:
            browser.close()

    def test_mobile_layout(self):
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.open()
        self.start()
        self.capture()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.locator('#download-link').scroll_into_view_if_needed()
        expect(self.page.locator('#download-link')).to_be_in_viewport()


if __name__ == '__main__':
    unittest.main(verbosity=2)
