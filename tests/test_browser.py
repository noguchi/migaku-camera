"""Real Chromium camera/JPEG checks; unavailable hardware states are simulated."""
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

    def open(self, active=True):
        response = self.page.goto(self.url)
        self.assertEqual(response.status, 200)
        if active:
            expect(self.page.locator('#capture-button')).to_be_enabled(timeout=10000)
            expect(self.page.locator('#camera-select option')).to_have_count(2)

    def start(self):
        self.page.locator('#start-button').click()
        expect(self.page.locator('#capture-button')).to_be_enabled(timeout=10000)
        expect(self.page.locator('#live-badge')).to_have_text('接続中')

    def poll(self, expression, arg=None):
        # wait_for_function uses eval internally, which the app's CSP prohibits.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.page.evaluate(expression, arg):
                return
            self.page.wait_for_timeout(40)
        self.fail(f'Timed out: {expression}')

    def capture(self):
        count = self.page.locator('.photo-card').count()
        self.page.locator('#capture-button').click()
        expect(self.page.locator('.photo-card')).to_have_count(count + 1)
        expect(self.page.locator('#capture-button')).to_be_enabled()
        # Lazy images below the fold load when visible; only inspect the newest.
        self.page.locator('.photo-image').first.scroll_into_view_if_needed()
        self.poll("() => {const img = document.querySelector('.photo-image'); return img.complete && img.naturalWidth > 0;}")

    def assert_jpeg(self, data, expected_size):
        self.assertEqual(data[:2], b'\xff\xd8')
        self.assertEqual(data[-2:], b'\xff\xd9')
        offset = 2
        while offset < len(data):
            self.assertEqual(data[offset], 0xff)
            while data[offset] == 0xff:
                offset += 1
            marker = data[offset]
            offset += 1
            if marker in (0xda, 0xd9):
                break
            length = struct.unpack('>H', data[offset:offset + 2])[0]
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                self.assertEqual((width, height), expected_size)
                return
            offset += length
        self.fail('JPEG has no frame dimensions')

    def test_auto_start_default_camera_at_maximum_native_resolution(self):
        self.open()
        self.assertEqual(self.page.evaluate('testConstraints.length'), 1)
        self.assertNotIn('deviceId', self.page.evaluate('testConstraints[0].video'))
        info = self.page.evaluate("() => {const t=testStreams[0].getVideoTracks()[0];return {settings:t.getSettings(),capabilities:t.getCapabilities()}}")
        self.assertEqual(info['settings']['width'], info['capabilities']['width']['max'])
        self.assertEqual(info['settings']['height'], info['capabilities']['height']['max'])
        self.assertEqual(info['settings']['resizeMode'], 'none')
        self.assertEqual(self.page.evaluate('testStreams[0].getAudioTracks().length'), 0)
        expect(self.page.locator('#start-button')).to_be_disabled()
        expect(self.page.locator('#status')).to_be_hidden()
        expect(self.page.locator('#photo-list')).to_be_empty()
        expect(self.page.locator('#photo-placeholder')).to_be_visible()
        expect(self.page.locator('#resolution-select')).to_have_count(0)
        expect(self.page.locator('.intro, .guide, .troubleshooting, footer, .hint, .privacy-badge')).to_have_count(0)

    def test_multiple_photos_latest_first_and_jpeg_downloads_after_stop(self):
        self.open()
        size = tuple(self.page.evaluate("() => [document.querySelector('#video').videoWidth, document.querySelector('#video').videoHeight]"))
        for _ in range(3):
            self.capture()
        ids = self.page.locator('.photo-card').evaluate_all('cards => cards.map(card => card.dataset.photoId)')
        self.assertEqual(ids, ['3', '2', '1'])
        expect(self.page.locator('#photo-count')).to_have_text('3')
        names = self.page.locator('.download').evaluate_all('links => links.map(link => link.download)')
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 0)
        self.page.locator('#stop-button').click()
        expect(self.page.locator('#capture-button')).to_be_disabled()
        self.assertEqual(self.page.evaluate("testStreams[0].getVideoTracks()[0].readyState"), 'ended')
        for link in self.page.locator('.download').all():
            with self.page.expect_download() as event:
                link.click()
            download = event.value
            self.assertRegex(download.suggested_filename, r'^migaku-camera-[0-9-]+\.jpg$')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / download.suggested_filename
                download.save_as(path)
                self.assert_jpeg(path.read_bytes(), size)
        self.assertEqual(self.page.locator('.photo-card').count(), 3)
        self.start()
        self.capture()
        expect(self.page.locator('.photo-card')).to_have_count(4)
        self.assertEqual(self.page.locator('.photo-card').first.get_attribute('data-photo-id'), '4')
        self.assert_no_external_requests()

    def assert_no_external_requests(self):
        host = urlsplit(self.url).netloc
        self.assertTrue(self.requests)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests), self.requests)
        self.assertTrue(all(urlsplit(url).scheme in ('http', 'https', 'blob') for _, url in self.requests), self.requests)
        network = [(method, url) for method, url in self.requests if urlsplit(url).scheme != 'blob']
        self.assertTrue(all(urlsplit(url).netloc == host for _, url in network), network)
        self.assertTrue(all(urlsplit(url).path.endswith(('/', '/app.js', '/style.css', '/index.html')) for _, url in network), network)
        origin_scheme = urlsplit(self.url).scheme
        self.assertTrue(all(urlsplit(url[5:]).scheme == origin_scheme and urlsplit(url[5:]).netloc == host for _, url in self.requests if urlsplit(url).scheme == 'blob'), self.requests)

    def test_individual_and_all_photo_deletion_releases_urls(self):
        self.open()
        for _ in range(3):
            self.capture()
        urls = self.page.locator('.download').evaluate_all('links => links.map(link => link.href)')
        self.page.locator('.delete-photo').nth(1).click()
        expect(self.page.locator('.photo-card')).to_have_count(2)
        self.assertEqual(self.page.locator('.photo-card').evaluate_all('cards => cards.map(c => c.dataset.photoId)'), ['3', '1'])
        self.assertEqual(self.page.evaluate('revokedUrls'), [urls[1]])
        self.page.locator('#clear-button').click()
        expect(self.page.locator('#photo-list')).to_be_empty()
        expect(self.page.locator('#photo-count')).to_have_text('0')
        expect(self.page.locator('#photo-placeholder')).to_be_visible()
        expect(self.page.locator('#clear-button')).to_be_disabled()
        self.assertEqual(set(self.page.evaluate('revokedUrls')), set(urls))

    def test_camera_switch_preserves_photos_and_uses_maximum_resolution(self):
        self.open()
        self.capture()
        first_id = self.page.locator('#camera-select').input_value()
        second_id = self.page.locator('#camera-select option').nth(1).get_attribute('value')
        self.assertNotEqual(first_id, second_id)
        self.page.locator('#camera-select').select_option(second_id)
        self.poll('testStreams.length === 2')
        expect(self.page.locator('#capture-button')).to_be_enabled()
        info = self.page.evaluate("() => {const t=testStreams[1].getVideoTracks()[0];return {settings:t.getSettings(),capabilities:t.getCapabilities()}}")
        self.assertEqual(info['settings']['deviceId'], second_id)
        self.assertEqual(info['settings']['width'], info['capabilities']['width']['max'])
        self.assertEqual(info['settings']['height'], info['capabilities']['height']['max'])
        self.assertEqual(self.page.evaluate('testStreams[0].getVideoTracks()[0].readyState'), 'ended')
        self.capture()
        expect(self.page.locator('.photo-card')).to_have_count(2)
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 0)

    def test_missing_camera_and_reconnect(self):
        self.page.add_init_script("""
          window.nativeEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
          window.nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.enumerateDevices = async () => [];
          navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('', 'NotFoundError'); };
        """)
        self.open(active=False)
        expect(self.page.locator('#status')).to_have_text('カメラが見つかりません')
        expect(self.page.locator('#camera-select')).to_be_disabled()
        expect(self.page.locator('#start-button')).to_be_enabled()
        self.page.evaluate('() => { navigator.mediaDevices.enumerateDevices = nativeEnumerate; navigator.mediaDevices.getUserMedia = nativeGetUserMedia; }')
        self.page.locator('#refresh-button').click()
        expect(self.page.locator('#camera-select option')).to_have_count(2)
        self.start()

    def test_permission_busy_and_unavailable_camera_errors(self):
        self.open()
        self.page.locator('#stop-button').click()
        cases = [('NotAllowedError', '許可されていません'), ('NotReadableError', '使用中'), ('OverconstrainedError', '選択したカメラを使用できません')]
        for name, message in cases:
            with self.subTest(error=name):
                self.page.evaluate("name => { navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('', name); }; }", name)
                self.page.locator('#start-button').click()
                expect(self.page.locator('#status')).to_contain_text(message)
                expect(self.page.locator('#start-button')).to_be_enabled()
                expect(self.page.locator('#capture-button')).to_be_disabled()
                expect(self.page.locator('#stop-button')).to_be_disabled()

    def test_pending_auto_start_cancel_releases_late_stream(self):
        self.page.add_init_script("""
          const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async constraints => {
            const stream = await original(constraints);
            return new Promise(resolve => { window.releasePending = () => resolve(stream); });
          };
        """)
        self.open(active=False)
        expect(self.page.locator('#stop-button')).to_be_enabled()
        expect(self.page.locator('#camera-select')).to_be_disabled()
        self.poll("typeof releasePending === 'function'")
        self.page.locator('#stop-button').click()
        expect(self.page.locator('#start-button')).to_be_enabled()
        self.page.evaluate('releasePending()')
        self.poll("testStreams[0].getVideoTracks()[0].readyState === 'ended'")
        self.assertIsNone(self.page.evaluate("document.querySelector('#video').srcObject"))
        expect(self.page.locator('#live-badge')).to_have_text('停止中')
        expect(self.page.locator('#status')).to_be_hidden()

    def test_camera_disconnect_and_page_cleanup(self):
        self.open()
        self.page.evaluate("testStreams[0].getVideoTracks()[0].dispatchEvent(new Event('ended'))")
        expect(self.page.locator('#status')).to_have_text('カメラの接続が終了しました')
        expect(self.page.locator('#capture-button')).to_be_disabled()
        self.assertEqual(self.page.evaluate("testStreams[0].getVideoTracks()[0].readyState"), 'ended')
        self.start()
        self.capture()
        self.capture()
        self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        self.assertTrue(self.page.evaluate("testStreams.every(s => s.getVideoTracks()[0].readyState === 'ended')"))
        expect(self.page.locator('#photo-list')).to_be_empty()
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 2)
        self.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
        expect(self.page.locator('#capture-button')).to_be_enabled()
        self.assertEqual(self.page.evaluate('testStreams.length'), 3)

    def test_clear_during_image_encoding_does_not_restore_photos(self):
        self.open()
        self.capture()
        self.page.evaluate("""() => {
          const original = HTMLCanvasElement.prototype.toBlob;
          HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
            original.call(this, blob => { window.finishPhoto = () => callback(blob); }, ...args);
          };
        }""")
        self.page.locator('#capture-button').click()
        self.poll("typeof finishPhoto === 'function'")
        self.page.locator('#clear-button').click()
        self.page.evaluate('finishPhoto()')
        expect(self.page.locator('#capture-button')).to_be_enabled()
        expect(self.page.locator('#photo-list')).to_be_empty()
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 1)

    def test_jpeg_encoding_failure_keeps_existing_photos(self):
        self.open()
        self.capture()
        self.page.evaluate('() => { HTMLCanvasElement.prototype.toBlob = callback => callback(null); }')
        self.page.locator('#capture-button').click()
        expect(self.page.locator('#status')).to_have_text('画像を作成できません')
        expect(self.page.locator('.photo-card')).to_have_count(1)
        expect(self.page.locator('#capture-button')).to_be_enabled()
        self.assertEqual(self.page.evaluate('revokedUrls.length'), 0)

    def test_unsupported_browser(self):
        self.page.add_init_script("Object.defineProperty(navigator, 'mediaDevices', {value: undefined})")
        self.open(active=False)
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

    def test_real_browser_permission_denial_on_auto_start(self):
        browser = self.playwright.chromium.launch(
            executable_path=os.environ.get('CHROMIUM_PATH', '/usr/bin/chromium'),
            headless=True, args=['--use-fake-device-for-media-stream'],
        )
        try:
            page = browser.new_page()
            page.goto(self.url)
            expect(page.locator('#status')).to_contain_text('許可されていません')
            expect(page.locator('#capture-button')).to_be_disabled()
        finally:
            browser.close()

    def test_full_viewport_video_and_independent_overlay_scroll(self):
        self.page.set_viewport_size({'width': 1440, 'height': 600})
        self.open()
        for _ in range(3):
            self.capture()
        video = self.page.locator('#video').bounding_box()
        self.assertEqual(video, {'x': 0, 'y': 0, 'width': 1440, 'height': 600})
        camera = self.page.locator('.camera-panel').bounding_box()
        gallery = self.page.locator('.photo-panel').bounding_box()
        self.assertGreater(gallery['x'], camera['x'] + camera['width'])
        self.assertTrue(self.page.evaluate("() => {const p=document.querySelector('.camera-panel'),r=p.getBoundingClientRect();return p.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}"))
        self.assertTrue(self.page.evaluate("() => {const p=document.querySelector('.photo-panel'),r=p.getBoundingClientRect();return p.contains(document.elementFromPoint(r.x+r.width/2,r.y+20));}"))
        self.page.evaluate("document.querySelector('.photo-panel').scrollTop = 9999")
        self.assertGreater(self.page.evaluate("document.querySelector('.photo-panel').scrollTop"), 0)
        expect(self.page.locator('#capture-button')).to_be_in_viewport()
        self.assertEqual(self.page.locator('#video').bounding_box(), video)
        self.capture()
        self.assertEqual(self.page.evaluate("document.querySelector('.photo-panel').scrollTop"), 0)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollHeight <= innerHeight'))

    def test_gallery_toggle_preserves_camera_and_photos(self):
        self.open()
        self.capture()
        self.page.locator('#gallery-toggle').click()
        expect(self.page.locator('#photo-panel')).to_be_hidden()
        expect(self.page.locator('#gallery-toggle')).to_have_attribute('aria-expanded', 'false')
        self.assertEqual(self.page.evaluate("testStreams[0].getVideoTracks()[0].readyState"), 'live')
        self.page.locator('#capture-button').click()
        expect(self.page.locator('.photo-card')).to_have_count(2)
        self.page.locator('#gallery-toggle').click()
        expect(self.page.locator('#photo-panel')).to_be_visible()
        expect(self.page.locator('#gallery-toggle')).to_have_attribute('aria-expanded', 'true')
        expect(self.page.locator('#photo-count')).to_have_text('2')

    def test_mobile_layout(self):
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.open()
        self.capture()
        self.capture()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.locator('.download').first.scroll_into_view_if_needed()
        expect(self.page.locator('.download').first).to_be_in_viewport()
        for width, height in [(375, 812), (320, 568), (667, 375)]:
            with self.subTest(viewport=(width, height)):
                self.page.set_viewport_size({'width': width, 'height': height})
                self.assertEqual(self.page.locator('#video').bounding_box(), {'x': 0, 'y': 0, 'width': width, 'height': height})
                camera = self.page.locator('.camera-panel').bounding_box()
                gallery = self.page.locator('.photo-panel').bounding_box()
                actions = self.page.locator('.camera-actions').bounding_box()
                self.assertGreater(gallery['x'], camera['x'] + camera['width'])
                self.assertLessEqual(camera['y'] + camera['height'], actions['y'])
                self.assertLessEqual(gallery['y'] + gallery['height'], actions['y'])
                expect(self.page.locator('#capture-button')).to_be_in_viewport()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
