'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const video = $('video');
  const camera = $('camera-select');
  const media = navigator.mediaDevices;
  const supported = window.isSecureContext && !!media?.getUserMedia;
  const cameraPriorities = ['teslong', 'usb', 'uvc', 'back'];
  const photos = new Map();
  let stream = null;
  let starting = false;
  let capturing = false;
  let generation = 0;
  let enumeration = 0;
  let photoGeneration = 0;
  let photoSequence = 0;
  let audioContext = null;
  let shutterBuffer = null;
  let pageActive = true;
  let manualCameraId = '';

  function status(message = '') {
    $('status').textContent = message;
    $('status').hidden = !message;
  }

  function controls() {
    const active = !!stream;
    camera.disabled = !supported || starting || !camera.value;
    video.setAttribute('aria-disabled', String(!canCapture()));
    video.hidden = !active;
    $('preview-placeholder').hidden = active;
  }

  function photoControls() {
    $('photo-placeholder').hidden = photos.size > 0;
  }

  function releaseStream() {
    if (stream) {
      for (const track of stream.getTracks()) {
        track.onended = null;
        track.stop();
      }
    }
    stream = null;
    video.srcObject = null;
  }

  function stop(message = '') {
    generation++;
    starting = false;
    releaseStream();
    controls();
    status(message);
  }

  const errors = {
    NotAllowedError: 'カメラの使用が許可されていません',
    SecurityError: 'カメラの使用が制限されています',
    NotFoundError: 'カメラが見つかりません',
    DevicesNotFoundError: 'カメラが見つかりません',
    NotReadableError: 'カメラが使用中、または読み取りできません',
    TrackStartError: 'カメラが使用中、または開始できません',
    OverconstrainedError: '選択したカメラを使用できません',
    AbortError: 'カメラの開始が中断されました'
  };

  function priorityCamera(devices) {
    for (const keyword of cameraPriorities) {
      const device = devices.find(device => device.deviceId && device.label.toLowerCase().includes(keyword));
      if (device) return device;
    }
  }

  async function refreshDevices(manual = false) {
    const request = ++enumeration;
    const state = generation;
    try {
      const devices = (await media.enumerateDevices()).filter(device => device.kind === 'videoinput');
      if (request !== enumeration || state !== generation || !pageActive) return false;
      const activeId = stream?.getVideoTracks()[0]?.getSettings().deviceId;
      const preferred = devices.find(device => device.deviceId && device.deviceId === manualCameraId)
        || priorityCamera(devices)
        || devices.find(device => device.deviceId === (activeId || camera.value))
        || devices[0];
      camera.replaceChildren();
      devices.forEach((device, index) => {
        const name = device.label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, '').trim();
        camera.add(new Option(name || `カメラ ${index + 1}`, device.deviceId));
      });
      if (!devices.length) {
        camera.add(new Option('カメラなし', ''));
        if (!stream && !starting && (manual || !$('status').textContent)) status(errors.NotFoundError);
      } else {
        camera.value = preferred.deviceId;
        if (manual && !stream && !starting) status();
      }
      if (activeId && !devices.some(device => device.deviceId === activeId)) stop('カメラの接続が終了しました');
      controls();
      return true;
    } catch (error) {
      if (request !== enumeration || state !== generation || !pageActive) return false;
      status(errors[error.name] || 'カメラ一覧を取得できません');
      controls();
      return false;
    }
  }

  async function start(useDefault = false) {
    if (!supported || starting || !pageActive) return;
    const request = ++generation;
    releaseStream();
    starting = true;
    controls();
    status();
    // Prefer the largest native camera format, without requiring an unsupported size.
    const constraints = { width: { ideal: 65535 }, height: { ideal: 65535 }, resizeMode: { ideal: 'none' } };
    if (!useDefault && camera.value) constraints.deviceId = { exact: camera.value };
    try {
      const acquired = await media.getUserMedia({ audio: false, video: constraints });
      if (request !== generation) {
        acquired.getTracks().forEach(track => track.stop());
        return;
      }
      stream = acquired;
      const track = stream.getVideoTracks()[0];
      track.onended = () => {
        if (stream === acquired) {
          stop('カメラの接続が終了しました');
          void refreshDevices();
        }
      };
      video.srcObject = stream;
      await video.play();
      if (request !== generation) return;
      await refreshDevices();
      if (request !== generation || !pageActive) return;
      starting = false;
      const activeId = track.getSettings().deviceId;
      if (activeId && camera.value && camera.value !== activeId) void start();
      else controls();
    } catch (error) {
      if (request !== generation) return;
      stop(errors[error.name] || 'カメラを開始できません');
      await refreshDevices();
    }
  }

  function deletePhoto(id) {
    const photo = photos.get(id);
    if (!photo) return;
    URL.revokeObjectURL(photo.url);
    photo.card.remove();
    photos.delete(id);
    photoControls();
  }

  function clearPhotos() {
    photoGeneration++;
    for (const id of photos.keys()) deletePhoto(id);
  }

  function canCapture() {
    return !!stream && !starting && !capturing && video.readyState >= 2 && !!video.videoWidth;
  }

  function prepareShutter() {
    try {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return null;
      audioContext ||= new Audio();
      // Resume during the click/key gesture, before JPEG encoding yields.
      const context = audioContext;
      const resumed = context.resume().catch(() => {});
      if (!shutterBuffer) {
        const bytes = Uint8Array.from(atob(window.MIGAKU_SHUTTER_WAV_BASE64), char => char.charCodeAt(0));
        shutterBuffer = context.decodeAudioData(bytes.buffer).catch(() => null);
      }
      return Promise.all([resumed, shutterBuffer]).then(([, buffer]) => buffer ? { context, buffer } : null).catch(() => null);
    } catch {
      return null;
    }
  }

  function playShutter(sound) {
    if (!sound || sound.context.state !== 'running') return;
    try {
      const { context, buffer } = sound;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      source.onended = () => source.disconnect();
      source.start();
    } catch {
      // Audio availability must not prevent saving the photo.
    }
  }

  async function capture() {
    if (!canCapture()) return;
    const shutter = prepareShutter();
    capturing = true;
    controls();
    const request = photoGeneration;
    const date = new Date();
    try {
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.95));
      if (request !== photoGeneration) return;
      if (!blob || blob.type !== 'image/jpeg') throw new Error('JPEG encoding failed');
      const id = ++photoSequence;
      const url = URL.createObjectURL(blob);
      const dateStamp = [date.getFullYear(), date.getMonth() + 1, date.getDate()].map(n => String(n).padStart(2, '0')).join('');
      const timeStamp = [date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join('');
      const card = $('photo-card-template').content.firstElementChild.cloneNode(true);
      card.dataset.photoId = id;
      const image = card.querySelector('.photo-image');
      image.alt = `撮影画像 ${id}`;
      image.width = canvas.width;
      image.height = canvas.height;
      image.src = url;
      const download = card.querySelector('.download');
      download.href = url;
      download.download = `migaku-${dateStamp}-${timeStamp}-${id}.jpg`;
      download.setAttribute('aria-label', `撮影画像 ${id} をJPGでダウンロード`);
      const remove = card.querySelector('.delete-photo');
      remove.setAttribute('aria-label', `撮影画像 ${id} を削除`);
      remove.addEventListener('click', () => {
        const next = card.nextElementSibling || card.previousElementSibling;
        deletePhoto(id);
        (next?.querySelector('.delete-photo') || video).focus({ preventScroll: true });
        $('photo-announcement').textContent = `${photos.size}枚`;
      });
      photos.set(id, { url, card });
      $('photo-list').prepend(card);
      $('photo-panel').scrollTop = 0;
      photoControls();
      $('photo-announcement').textContent = `${photos.size}枚`;
      void shutter?.then(sound => {
        if (request === photoGeneration) playShutter(sound);
      });
    } catch {
      status('画像を作成できません');
    } finally {
      capturing = false;
      controls();
    }
  }

  video.addEventListener('click', () => {
    video.focus({ preventScroll: true });
    void capture();
  });
  document.addEventListener('keydown', event => {
    if (event.code !== 'Space' || event.repeat || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.target.closest('button, select, input, textarea, a, [contenteditable]:not([contenteditable="false"])')) return;
    event.preventDefault();
    void capture();
  });
  async function reconnect(manual = false) {
    if (!pageActive) return;
    const refreshed = await refreshDevices(manual);
    if (!refreshed || !pageActive || starting) return;
    // Before permission is granted, enumeration may hide device IDs.
    const activeId = stream?.getVideoTracks()[0]?.getSettings().deviceId;
    if ((!stream && (manual || camera.value)) || (activeId && camera.value && camera.value !== activeId)) void start();
  }
  camera.addEventListener('change', () => {
    manualCameraId = camera.value;
    void start();
  });
  const qrToggle = $('qr-toggle');
  qrToggle.addEventListener('click', () => {
    const expanded = qrToggle.getAttribute('aria-expanded') !== 'true';
    qrToggle.setAttribute('aria-expanded', String(expanded));
    qrToggle.setAttribute('aria-label', expanded ? 'QRコードを元のサイズに戻す' : 'QRコードを4倍に拡大');
  });
  const fullscreen = $('fullscreen-toggle');
  let fullscreenPending = false;
  function fullscreenControls() {
    const active = !!document.fullscreenElement;
    const available = !!document.fullscreenEnabled && !!document.documentElement.requestFullscreen && !!document.exitFullscreen;
    const label = !available ? '全画面表示に対応していません' : active ? '全画面表示を解除' : '全画面表示';
    fullscreen.disabled = !available;
    fullscreen.setAttribute('aria-busy', String(fullscreenPending));
    fullscreen.setAttribute('aria-pressed', String(active));
    fullscreen.setAttribute('aria-label', label);
    fullscreen.title = label;
    fullscreen.querySelector('use').setAttribute('href', active ? '#icon-fullscreen-exit' : '#icon-fullscreen-enter');
  }
  fullscreen.addEventListener('click', async () => {
    if (fullscreenPending) return;
    fullscreenPending = true;
    fullscreenControls();
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      status('全画面表示を切り替えられません');
    } finally {
      fullscreenPending = false;
      fullscreenControls();
    }
  });
  document.addEventListener('fullscreenchange', fullscreenControls);
  fullscreenControls();
  function showGallery(visible) {
    const panel = $('photo-panel');
    panel.hidden = !visible;
    $('gallery-reveal').hidden = visible;
    $('photo-heading-toggle').setAttribute('aria-expanded', String(visible));
    $('gallery-reveal').setAttribute('aria-expanded', String(visible));
    (visible ? $('photo-heading-toggle') : video.hidden ? $('gallery-reveal') : video).focus({ preventScroll: true });
  }
  $('photo-panel').addEventListener('click', event => {
    if (!event.target.closest('.photo-image, .photo-actions')) showGallery(false);
  });
  $('gallery-reveal').addEventListener('click', () => showGallery(true));
  video.addEventListener('loadeddata', controls);
  video.addEventListener('resize', controls);
  window.addEventListener('pagehide', () => {
    pageActive = false;
    stop();
    clearPhotos();
    if (audioContext) void audioContext.close().catch(() => {});
    audioContext = null;
    shutterBuffer = null;
  });
  window.addEventListener('pageshow', event => {
    pageActive = true;
    if (supported && event.persisted) void start(true);
  });
  if (supported) {
    media.addEventListener('devicechange', () => void reconnect(true));
    // Retry when the user restores camera permission, without a retry button.
    if (navigator.permissions?.query) {
      void navigator.permissions.query({ name: 'camera' }).then(permission => {
        permission.addEventListener('change', () => {
          if (permission.state === 'granted' && !stream && !starting) void reconnect(true);
        });
      }).catch(() => {});
    }
    void start(true);
  } else {
    status(window.isSecureContext ? 'カメラ機能に対応していません' : 'HTTPSが必要です');
  }
  controls();
  photoControls();
})();
