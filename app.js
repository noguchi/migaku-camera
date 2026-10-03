'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const video = $('video');
  const camera = $('camera-select');
  const resolution = $('resolution-select');
  const startButton = $('start-button');
  const stopButton = $('stop-button');
  const captureButton = $('capture-button');
  const media = navigator.mediaDevices;
  const supported = window.isSecureContext && !!media?.getUserMedia;
  let stream = null;
  let starting = false;
  let capturing = false;
  let generation = 0;
  let enumeration = 0;
  let photoGeneration = 0;
  let photoUrl = null;

  function status(message, error = false) {
    $('status').textContent = message;
    $('status').classList.toggle('error', error);
  }

  function controls() {
    const active = !!stream;
    startButton.disabled = !supported || starting || active;
    startButton.textContent = starting ? 'カメラを開始しています…' : 'カメラを開始';
    stopButton.disabled = !active && !starting;
    camera.disabled = !supported || starting || !camera.options.length || !camera.value;
    resolution.disabled = !supported || starting;
    $('refresh-button').disabled = !supported || starting;
    captureButton.disabled = !active || starting || capturing || video.readyState < 2 || !video.videoWidth;
    $('live-badge').textContent = starting ? '準備中' : active ? '接続中' : '停止中';
    $('live-badge').classList.toggle('live', active);
    video.hidden = !active;
    $('preview-placeholder').hidden = active;
    $('video-size').textContent = active && video.videoWidth ? `${video.videoWidth} × ${video.videoHeight} px` : '映像なし';
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

  function stop(message = 'カメラを停止しました。撮影済みの写真は保存できます。', error = false) {
    generation++;
    starting = false;
    releaseStream();
    controls();
    status(message, error);
  }

  const errors = {
    NotAllowedError: 'カメラの使用が許可されていません。アドレスバーのサイト設定とOSの設定でカメラを許可し、もう一度開始してください。',
    SecurityError: 'カメラがセキュリティ設定で制限されています。サイトとOSのカメラ権限を確認してください。',
    NotFoundError: 'カメラが見つかりません。USBカメラの接続を確認し「再検出」を押してください。',
    DevicesNotFoundError: 'カメラが見つかりません。USB接続を確認してください。',
    NotReadableError: 'カメラを読み取れません。他のアプリやタブで使用中の場合は停止してください。USB接続やOSのカメラ権限も確認し、再度開始してください。',
    TrackStartError: 'カメラを開始できません。他のアプリで使用中の場合は停止して、再度開始してください。',
    OverconstrainedError: '選択したカメラまたは解像度を使用できません。「再検出」でカメラを確認し、解像度を「カメラの標準」にして再度開始してください。',
    AbortError: 'カメラの開始が中断されました。USB接続を確認して、もう一度開始してください。'
  };

  async function refreshDevices(announce = false) {
    const request = ++enumeration;
    try {
      const devices = (await media.enumerateDevices()).filter(device => device.kind === 'videoinput');
      if (request !== enumeration) return;
      const activeId = stream?.getVideoTracks()[0]?.getSettings().deviceId;
      const preferred = activeId || camera.value;
      camera.replaceChildren();
      devices.forEach((device, index) => camera.add(new Option(device.label || `カメラ ${index + 1}（開始後に名前を表示）`, device.deviceId)));
      if (!devices.length) {
        camera.add(new Option('カメラが見つかりません', ''));
        if (!stream && !starting) status(errors.NotFoundError, true);
      } else {
        if (devices.some(device => device.deviceId === preferred)) camera.value = preferred;
        if (announce && !starting && !stream) status('カメラを検出しました。使用するカメラを選んで開始してください。');
      }
      if (activeId && !devices.some(device => device.deviceId === activeId)) {
        stop('使用していたカメラの接続が失われました。USB接続を確認し、カメラを選んで再度開始してください。', true);
      }
      controls();
    } catch (error) {
      if (request !== enumeration) return;
      status(errors[error.name] || 'カメラ一覧を取得できません。「再検出」を押してもう一度お試しください。', true);
      controls();
    }
  }

  async function start() {
    if (!supported) return;
    const request = ++generation;
    releaseStream();
    starting = true;
    controls();
    status('カメラの使用許可を確認しています。ブラウザーの許可画面で「許可」を選んでください。');
    const dimensions = { qvga: [320, 240], vga: [640, 480], hd: [1280, 720], fullhd: [1920, 1080] }[resolution.value];
    const constraints = {};
    if (camera.value) constraints.deviceId = { exact: camera.value };
    if (dimensions) {
      constraints.width = { exact: dimensions[0] };
      constraints.height = { exact: dimensions[1] };
    }
    let acquired = null;
    try {
      acquired = await media.getUserMedia({ audio: false, video: constraints });
      if (request !== generation) {
        acquired.getTracks().forEach(track => track.stop());
        return;
      }
      stream = acquired;
      const track = stream.getVideoTracks()[0];
      track.onended = () => {
        if (stream === acquired) {
          stop('カメラの接続が終了しました。USB接続や他のアプリの使用状況を確認して、再度開始してください。', true);
          void refreshDevices();
        }
      };
      video.srcObject = stream;
      await video.play();
      if (request !== generation) return;
      starting = false;
      status('カメラを開始しました。プレビューを確認して「静止画を撮影」を押してください。');
      controls();
      await refreshDevices();
    } catch (error) {
      if (request !== generation) return;
      stop(errors[error.name] || 'カメラを開始できません。接続と権限を確認して、もう一度お試しください。', true);
    }
  }

  function clearPhoto() {
    photoGeneration++;
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    photoUrl = null;
    $('photo').removeAttribute('src');
    $('photo').hidden = true;
    $('photo-placeholder').hidden = false;
    $('download-link').hidden = true;
    $('download-link').removeAttribute('href');
    $('clear-button').disabled = true;
    $('photo-info').textContent = '撮影し直すと、前の写真は置き換わります。';
  }

  async function capture() {
    if (!stream || video.readyState < 2 || !video.videoWidth || capturing) return;
    capturing = true;
    controls();
    const request = ++photoGeneration;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (request !== photoGeneration) return;
      if (!blob) throw new Error('PNG encoding failed');
      if (photoUrl) URL.revokeObjectURL(photoUrl);
      photoUrl = URL.createObjectURL(blob);
      const date = new Date();
      const stamp = [date.getFullYear(), date.getMonth() + 1, date.getDate(), date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join('-');
      $('photo').src = photoUrl;
      $('photo').hidden = false;
      $('photo-placeholder').hidden = true;
      $('download-link').href = photoUrl;
      $('download-link').download = `migaku-camera-${stamp}.png`;
      $('download-link').hidden = false;
      $('clear-button').disabled = false;
      $('photo-info').textContent = `${canvas.width} × ${canvas.height} px ・ ${date.toLocaleTimeString('ja-JP')} 撮影`;
      $('photo-status').textContent = '撮影しました。内容を確認してPNGをダウンロードしてください。';
    } catch {
      $('photo-status').textContent = '画像を作成できませんでした。もう一度撮影してください。';
    } finally {
      capturing = false;
      controls();
    }
  }

  startButton.addEventListener('click', () => void start());
  stopButton.addEventListener('click', () => stop());
  captureButton.addEventListener('click', () => void capture());
  $('refresh-button').addEventListener('click', () => void refreshDevices(true));
  camera.addEventListener('change', () => { if (stream) void start(); });
  resolution.addEventListener('change', () => { if (stream) void start(); });
  $('clear-button').addEventListener('click', () => { clearPhoto(); $('photo-status').textContent = '写真を消去しました。'; });
  video.addEventListener('loadeddata', controls);
  video.addEventListener('resize', controls);
  window.addEventListener('pagehide', () => { stop(); clearPhoto(); });
  window.addEventListener('pageshow', event => { if (supported && event.persisted) void refreshDevices(true); });
  if (supported) {
    media.addEventListener('devicechange', () => void refreshDevices(true));
    void refreshDevices(true);
  } else {
    status(window.isSecureContext ? 'このブラウザーはカメラ機能に対応していません。最新版のChromeまたはEdgeをお使いください。' : 'カメラを使用するにはHTTPSでこのページを開いてください。', true);
  }
  controls();
})();
