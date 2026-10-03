'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const video = $('video');
  const camera = $('camera-select');
  const media = navigator.mediaDevices;
  const supported = window.isSecureContext && !!media?.getUserMedia;
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

  function status(message = '') {
    $('status').textContent = message;
    $('status').hidden = !message;
  }

  function controls() {
    const active = !!stream;
    camera.disabled = !supported || starting || !camera.value;
    $('refresh-button').disabled = !supported || starting;
    video.setAttribute('aria-disabled', String(!canCapture()));
    $('live-badge').textContent = starting ? '準備中' : active ? '接続中' : '停止中';
    $('live-badge').classList.toggle('live', active);
    video.hidden = !active;
    $('preview-placeholder').hidden = active;
    $('video-size').textContent = active && video.videoWidth ? `${video.videoWidth} × ${video.videoHeight} px` : '';
  }

  function photoControls() {
    $('photo-count').textContent = photos.size;
    $('photo-placeholder').hidden = photos.size > 0;
    $('clear-button').disabled = photos.size === 0;
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

  async function refreshDevices(manual = false) {
    const request = ++enumeration;
    try {
      const devices = (await media.enumerateDevices()).filter(device => device.kind === 'videoinput');
      if (request !== enumeration) return;
      const activeId = stream?.getVideoTracks()[0]?.getSettings().deviceId;
      const preferred = activeId || camera.value;
      camera.replaceChildren();
      devices.forEach((device, index) => camera.add(new Option(device.label || `カメラ ${index + 1}`, device.deviceId)));
      if (!devices.length) {
        camera.add(new Option('カメラなし', ''));
        if (!stream && !starting && (manual || !$('status').textContent)) status(errors.NotFoundError);
      } else {
        if (devices.some(device => device.deviceId === preferred)) camera.value = preferred;
        if (manual && !stream && !starting) status();
      }
      if (activeId && !devices.some(device => device.deviceId === activeId)) stop('カメラの接続が終了しました');
      controls();
    } catch (error) {
      if (request !== enumeration) return;
      status(errors[error.name] || 'カメラ一覧を取得できません');
      controls();
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
      starting = false;
      controls();
      await refreshDevices();
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
      const stamp = [date.getFullYear(), date.getMonth() + 1, date.getDate(), date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join('-');
      const card = document.createElement('li');
      card.className = 'photo-card';
      card.dataset.photoId = id;
      const image = document.createElement('img');
      image.className = 'photo-image';
      image.alt = `撮影画像 ${id}`;
      image.width = canvas.width;
      image.height = canvas.height;
      image.loading = 'lazy';
      image.src = url;
      const meta = document.createElement('div');
      meta.className = 'photo-meta';
      const size = document.createElement('span');
      size.textContent = `${canvas.width} × ${canvas.height} px`;
      const time = document.createElement('time');
      time.dateTime = date.toISOString();
      time.textContent = date.toLocaleTimeString('ja-JP');
      meta.append(size, time);
      const actions = document.createElement('div');
      actions.className = 'photo-actions';
      const download = document.createElement('a');
      download.className = 'button primary download';
      download.href = url;
      download.download = `migaku-camera-${stamp}-${String(date.getMilliseconds()).padStart(3, '0')}-${id}.jpg`;
      download.textContent = 'JPG保存';
      download.setAttribute('aria-label', `撮影画像 ${id} をJPGでダウンロード`);
      const remove = document.createElement('button');
      remove.className = 'button subtle delete-photo';
      remove.type = 'button';
      remove.textContent = '削除';
      remove.setAttribute('aria-label', `撮影画像 ${id} を削除`);
      remove.addEventListener('click', () => {
        const next = card.nextElementSibling || card.previousElementSibling;
        deletePhoto(id);
        (next?.querySelector('.delete-photo') || video).focus({ preventScroll: true });
        $('photo-announcement').textContent = `${photos.size}枚`;
      });
      actions.append(download, remove);
      card.append(image, meta, actions);
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
    const request = generation;
    await refreshDevices(manual);
    // Before permission is granted, enumeration may hide device IDs.
    if (pageActive && request === generation && !stream && !starting && (manual || camera.value)) void start();
  }
  $('refresh-button').addEventListener('click', () => void reconnect(true));
  camera.addEventListener('change', () => void start());
  $('clear-button').addEventListener('click', () => { clearPhotos(); $('photo-announcement').textContent = '0枚'; });
  $('gallery-toggle').addEventListener('click', () => {
    const panel = $('photo-panel');
    panel.hidden = !panel.hidden;
    $('gallery-toggle').setAttribute('aria-expanded', String(!panel.hidden));
  });
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
    media.addEventListener('devicechange', () => void reconnect());
    void start(true);
  } else {
    status(window.isSecureContext ? 'カメラ機能に対応していません' : 'HTTPSが必要です');
  }
  controls();
  photoControls();
})();
