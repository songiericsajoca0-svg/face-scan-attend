(() => {
  "use strict";

  // Shared UI ---------------------------------------------------------------
  const body = document.body;
  const menuButton = document.getElementById("menuButton");
  const navScrim = document.getElementById("navScrim");
  const closeMenu = () => {
    body.classList.remove("menu-open");
    menuButton?.setAttribute("aria-expanded", "false");
  };
  menuButton?.addEventListener("click", () => {
    const willOpen = !body.classList.contains("menu-open");
    body.classList.toggle("menu-open", willOpen);
    menuButton.setAttribute("aria-expanded", String(willOpen));
  });
  navScrim?.addEventListener("click", closeMenu);
  document.querySelectorAll(".flash-close").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });
  window.setTimeout(() => document.querySelectorAll(".flash").forEach((item) => item.remove()), 6500);

  const stopStream = (stream) => {
    if (stream) stream.getTracks().forEach((track) => track.stop());
  };

  const cameraErrorText = (error) => {
    if (error?.name === "NotAllowedError") return "Camera permission was denied. Select the lock or camera icon in your browser, choose Allow, then try again.";
    if (error?.name === "NotFoundError") return "No camera was found on this laptop.";
    if (error?.name === "NotReadableError") return "The camera is being used by another application. Close Zoom, Meet, or another camera app first.";
    return "The camera could not be opened. Check the browser permission and laptop camera.";
  };

  const getCamera = () => navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } },
  });

  // Local browser face-recognition models ----------------------------------
  let modelPromise = null;
  const defaultModelUrl = window.FaceScanConfig?.modelUrl || "/static/models";
  const STABLE_MATCH_FRAMES = 3; // Automatic frames, not extra registration captures.

  async function loadFaceModels(modelUrl = defaultModelUrl) {
    if (!window.faceapi) {
      throw new Error("Face recognition files could not be loaded. Make sure the local face-model files are complete.");
    }
    if (!modelPromise) {
      modelPromise = Promise.all([
        window.faceapi.nets.tinyFaceDetector.loadFromUri(modelUrl),
        window.faceapi.nets.faceLandmark68TinyNet.loadFromUri(modelUrl),
        window.faceapi.nets.faceRecognitionNet.loadFromUri(modelUrl),
      ]);
    }
    return modelPromise;
  }

  const detectorOptions = () => new window.faceapi.TinyFaceDetectorOptions({
    inputSize: 320,
    scoreThreshold: 0.52,
  });

  async function detectFacesWithDescriptors(video) {
    return window.faceapi
      .detectAllFaces(video, detectorOptions())
      .withFaceLandmarks(true)
      .withFaceDescriptors();
  }

  function drawFaces(video, overlay, faces, mode = "neutral") {
    if (!overlay || !video.videoWidth || !video.videoHeight) return;
    if (overlay.width !== video.videoWidth || overlay.height !== video.videoHeight) {
      overlay.width = video.videoWidth;
      overlay.height = video.videoHeight;
    }
    const context = overlay.getContext("2d");
    context.clearRect(0, 0, overlay.width, overlay.height);
    let colour = "#ffbf66";
    if (faces.length === 1 && mode === "matched") colour = "#84f5c6";
    if (faces.length === 1 && mode === "unknown") colour = "#ffd166";
    context.lineWidth = Math.max(3, Math.round(video.videoWidth / 250));
    context.strokeStyle = colour;
    context.fillStyle = `${colour}26`;
    faces.forEach((face) => {
      const box = face.detection?.box || face.box;
      if (!box) return;
      context.fillRect(box.x, box.y, box.width, box.height);
      context.strokeRect(box.x, box.y, box.width, box.height);
    });
  }

  function createRecognitionMonitor(video, overlay, onFaces) {
    let active = false;
    let timer = null;
    let latestFaces = [];
    const clear = () => {
      if (!overlay) return;
      overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
    };
    const loop = async () => {
      if (!active) return;
      try {
        if (video.readyState >= HTMLMediaElement.HAVE_ENOUGH_DATA) {
          latestFaces = await detectFacesWithDescriptors(video);
          onFaces(latestFaces, (mode) => drawFaces(video, overlay, latestFaces, mode));
        }
      } catch (error) {
        console.warn("Face detection frame skipped", error);
        onFaces([], () => clear(), error);
      }
      if (active) timer = window.setTimeout(loop, 240);
    };
    return {
      start() { active = true; loop(); },
      stop() { active = false; latestFaces = []; if (timer) window.clearTimeout(timer); timer = null; clear(); },
      getLatestFace() { return latestFaces.length === 1 ? latestFaces[0] : null; },
    };
  }

  function normaliseDescriptor(descriptor) {
    const values = Array.from(descriptor || [], Number);
    const magnitude = Math.hypot(...values);
    if (!values.length || !Number.isFinite(magnitude) || magnitude < 0.00001) return null;
    return new Float32Array(values.map((value) => value / magnitude));
  }

  function cropFaceFromVideo(video, face) {
    if (!video?.videoWidth || !video?.videoHeight || !face) return null;
    const box = face.detection?.box || face.box;
    if (!box) return null;
    const padding = Math.max(box.width, box.height) * 0.36;
    const sourceX = Math.max(0, Math.floor(box.x - padding));
    const sourceY = Math.max(0, Math.floor(box.y - padding));
    const sourceRight = Math.min(video.videoWidth, Math.ceil(box.x + box.width + padding));
    const sourceBottom = Math.min(video.videoHeight, Math.ceil(box.y + box.height + padding));
    const sourceWidth = sourceRight - sourceX;
    const sourceHeight = sourceBottom - sourceY;
    if (sourceWidth < 20 || sourceHeight < 20) return null;

    // A compact profile image is sufficient because matching uses the separate
    // 128-value descriptor. Keeping captures small also fits Vercel requests.
    const largestSide = 360;
    const scale = Math.min(1, largestSide / Math.max(sourceWidth, sourceHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(sourceWidth * scale));
    canvas.height = Math.max(1, Math.round(sourceHeight * scale));
    canvas.getContext("2d").drawImage(video, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.8);
  }

  const setIndicator = (element, message, type = "") => {
    if (!element) return;
    element.textContent = message;
    element.className = `face-indicator ${type}`.trim();
  };

  // Registration: one face capture -----------------------------------------
  const registrationCamera = document.getElementById("registrationCamera");
  if (registrationCamera) {
    const video = document.getElementById("registrationVideo");
    const overlay = document.getElementById("registrationOverlay");
    const preview = document.getElementById("registrationPreview");
    const placeholder = document.getElementById("registrationPlaceholder");
    const indicator = document.getElementById("registrationFaceIndicator");
    const photoInput = document.getElementById("facePhotoData");
    const descriptorInput = document.getElementById("faceDescriptorsData");
    const startButton = document.getElementById("startRegistrationCamera");
    const captureButton = document.getElementById("captureRegistrationPhoto");
    const retakeButton = document.getElementById("retakeRegistrationPhoto");
    const dots = [...document.querySelectorAll("#registrationProgress .sample-dot")];
    const instruction = document.getElementById("registrationInstruction");
    const poseArrow = document.getElementById("registrationPoseArrow");
    const poseLabel = document.getElementById("registrationPoseLabel");
    const form = document.getElementById("studentForm");
    const requiredSamples = Math.max(1, Number(registrationCamera.dataset.requiredSamples || 1));
    const hasExistingProfile = Boolean(preview?.getAttribute("src")) && !descriptorInput.value;
    let stream = null;
    let monitor = null;
    let samples = [];
    let faceReady = false;

    const renderProgress = () => {
      dots.forEach((dot, index) => dot.classList.toggle("done", index < samples.length));
      if (samples.length >= requiredSamples) {
        instruction.textContent = "Face capture complete. The profile is ready to save.";
        if (poseArrow) poseArrow.textContent = "✓";
        if (poseLabel) poseLabel.textContent = "Face captured";
      } else if (hasExistingProfile && samples.length === 0) {
        instruction.textContent = "An existing face profile will be kept. Select Recapture face to replace it.";
        if (poseArrow) poseArrow.textContent = "↻";
        if (poseLabel) poseLabel.textContent = "Existing profile";
      } else {
        instruction.textContent = "Look straight ahead and keep one face in the camera frame.";
        if (poseArrow) poseArrow.textContent = "↑";
        if (poseLabel) poseLabel.textContent = "Look straight ahead";
      }
      captureButton.textContent = samples.length >= requiredSamples ? "✓ Face captured" : "▣ Capture face";
      captureButton.disabled = !faceReady || samples.length >= requiredSamples;
    };

    const stopRegistrationCamera = () => {
      monitor?.stop();
      monitor = null;
      stopStream(stream);
      stream = null;
      video.srcObject = null;
      video.classList.add("hidden");
      overlay.classList.add("hidden");
      indicator.classList.add("hidden");
      captureButton.classList.add("hidden");
      captureButton.disabled = true;
      faceReady = false;
    };

    const finishRegistration = () => {
      stopRegistrationCamera();
      preview.classList.remove("hidden");
      placeholder.classList.add("hidden");
      startButton.classList.add("hidden");
      retakeButton.classList.remove("hidden");
      renderProgress();
    };

    const startRegistrationCamera = async () => {
      try {
        startButton.disabled = true;
        startButton.textContent = "Loading face model…";
        await loadFaceModels(registrationCamera.dataset.modelUrl);
        stopRegistrationCamera();
        samples = [];
        photoInput.value = "";
        descriptorInput.value = "";
        renderProgress();
        stream = await getCamera();
        video.srcObject = stream;
        await video.play();
        preview.classList.add("hidden");
        placeholder.classList.add("hidden");
        video.classList.remove("hidden");
        overlay.classList.remove("hidden");
        indicator.classList.remove("hidden");
        startButton.classList.add("hidden");
        retakeButton.classList.add("hidden");
        captureButton.classList.remove("hidden");
        setIndicator(indicator, "Waiting for one face…");
        monitor = createRecognitionMonitor(video, overlay, (faces, draw, error) => {
          faceReady = faces.length === 1 && !error;
          if (faces.length === 1 && !error) {
            draw("matched");
            setIndicator(indicator, "One face found — ready to capture.", "ready");
          } else if (faces.length > 1) {
            draw("unknown");
            setIndicator(indicator, "Only one face must be inside the camera frame.", "error");
          } else if (error) {
            draw("unknown");
            setIndicator(indicator, "Face detection has a problem. Restart the camera.", "error");
          } else {
            draw("unknown");
            setIndicator(indicator, "Look straight ahead at the camera.");
          }
          renderProgress();
        });
        monitor.start();
      } catch (error) {
        stopRegistrationCamera();
        if (photoInput.value || preview.getAttribute("src")) preview.classList.remove("hidden");
        else placeholder.classList.remove("hidden");
        startButton.classList.remove("hidden");
        window.alert(error.message?.startsWith("Face recognition") ? error.message : cameraErrorText(error));
      } finally {
        startButton.disabled = false;
        startButton.textContent = "◉ Start face capture";
      }
    };

    const captureFace = () => {
      const face = faceReady ? monitor?.getLatestFace() : null;
      const image = cropFaceFromVideo(video, face);
      const descriptor = face?.descriptor ? normaliseDescriptor(face.descriptor) : null;
      if (!face || !image || !descriptor) {
        window.alert("Keep one clear face in the camera frame and wait until the Capture face button is ready.");
        return;
      }
      samples = [Array.from(descriptor)];
      photoInput.value = image;
      descriptorInput.value = JSON.stringify(samples);
      preview.src = image;
      finishRegistration();
    };

    startButton?.addEventListener("click", startRegistrationCamera);
    retakeButton?.addEventListener("click", startRegistrationCamera);
    captureButton?.addEventListener("click", captureFace);
    form?.addEventListener("submit", (event) => {
      const hasFreshFace = (() => {
        try { return JSON.parse(descriptorInput.value || "[]").length >= requiredSamples; } catch { return false; }
      })();
      if (!hasFreshFace && !hasExistingProfile) {
        event.preventDefault();
        window.alert("Capture one face before registering the student.");
      }
    });
    renderProgress();
    window.addEventListener("beforeunload", stopRegistrationCamera);
  }

  // Attendance: stable 1:N face match, then automatic Time In or Check Out -
  const attendanceApp = document.getElementById("attendanceApp");
  if (!attendanceApp) return;

  const video = document.getElementById("attendanceVideo");
  const overlay = document.getElementById("attendanceOverlay");
  const placeholder = document.getElementById("attendancePlaceholder");
  const indicator = document.getElementById("attendanceFaceIndicator");
  const startButton = document.getElementById("startAttendanceCamera");
  const stopButton = document.getElementById("stopAttendanceCamera");
  const scanMessage = document.getElementById("scanMessage");
  const result = document.getElementById("matchResult");
  const resultHeading = document.getElementById("matchResultHeading");
  const avatar = document.getElementById("matchAvatar");
  const name = document.getElementById("matchName");
  const number = document.getElementById("matchNumber");
  const gender = document.getElementById("matchGender");
  const grade = document.getElementById("matchGrade");
  const section = document.getElementById("matchSection");
  const status = document.getElementById("matchStatus");
  const resultMessage = document.getElementById("matchMessage");
  const nextButton = document.getElementById("scanNextFace");
  const timeInValue = document.getElementById("matchTimeIn");
  const timeOutValue = document.getElementById("matchTimeOut");
  const modeButtons = [...document.querySelectorAll(".mode-button[data-mode]")];
  const scannerModeText = document.getElementById("scannerModeText");
  const scheduleModeLabel = document.getElementById("scheduleModeLabel");
  const scheduleMainLabel = document.getElementById("scheduleMainLabel");
  const scheduleSubLabel = document.getElementById("scheduleSubLabel");
  const guideModeText = document.getElementById("guideModeText");
  const guideActionText = document.getElementById("guideActionText");
  const timeInCutoff = attendanceApp.dataset.timeInCutoff || "08:00";
  const checkoutTime = attendanceApp.dataset.checkoutTime || "17:00";
  const clockLabel = (rawTime) => {
    const [hourText, minuteText = "00"] = String(rawTime).split(":");
    const hour = Number(hourText);
    if (!Number.isFinite(hour)) return rawTime;
    return `${hour % 12 || 12}:${minuteText} ${hour >= 12 ? "PM" : "AM"}`;
  };

  let stream = null;
  let monitor = null;
  let matcher = null;
  let profilesById = new Map();
  let matchThreshold = 0.48;
  let stableId = null;
  let stableFrames = 0;
  let locked = false;
  let scanMode = attendanceApp.dataset.defaultMode === "time_out" ? "time_out" : "time_in";

  const setMessage = (message, type = "") => {
    scanMessage.textContent = message;
    scanMessage.className = `scan-message ${type}`.trim();
  };

  const modeLabel = () => scanMode === "time_out" ? "Check Out" : "Time In";

  const applyMode = (mode) => {
    scanMode = mode === "time_out" ? "time_out" : "time_in";
    stableId = null;
    stableFrames = 0;
    modeButtons.forEach((button) => button.classList.toggle("active", button.dataset.mode === scanMode));
    if (scannerModeText) scannerModeText.textContent = modeLabel();
    if (scanMode === "time_out") {
      if (scheduleModeLabel) scheduleModeLabel.textContent = "Check Out mode · Dismissal";
      if (scheduleMainLabel) scheduleMainLabel.textContent = `Check Out from ${clockLabel(checkoutTime)}`;
      if (scheduleSubLabel) scheduleSubLabel.textContent = "A student must have a Time In record before Check Out can be recorded.";
      if (guideModeText) guideModeText.textContent = "Dismissal";
      if (guideActionText) guideActionText.textContent = "A successful face match records Check Out automatically.";
      setMessage(`Check Out mode selected. It is available from ${clockLabel(checkoutTime)}.`);
    } else {
      if (scheduleModeLabel) scheduleModeLabel.textContent = "Time In mode · Arrival";
      if (scheduleMainLabel) scheduleMainLabel.textContent = `Present until ${clockLabel(timeInCutoff)}`;
      if (scheduleSubLabel) scheduleSubLabel.textContent = `A Time In after ${clockLabel(timeInCutoff)} is recorded as Late.`;
      if (guideModeText) guideModeText.textContent = "Arrival";
      if (guideActionText) guideActionText.textContent = "A successful face match records Time In and the Present/Late status automatically.";
      setMessage(`Time In mode selected. Students are Present at or before ${clockLabel(timeInCutoff)} and Late after that.`);
    }
  };

  const setAvatar = (student) => {
    avatar.replaceChildren();
    if (student.photo_url) {
      const image = document.createElement("img");
      image.src = student.photo_url;
      image.alt = `Profile photo of ${student.full_name}`;
      avatar.append(image);
    } else {
      avatar.textContent = student.initials || "ST";
    }
  };

  const showMatch = (student, distance, state, message, timing = {}) => {
    setAvatar(student);
    name.textContent = student.full_name;
    number.textContent = student.student_number;
    gender.textContent = student.gender;
    grade.textContent = student.grade_level;
    section.textContent = student.section_name;
    status.textContent = state;
    if (timeInValue) timeInValue.textContent = timing.checked_in_at || "—";
    if (timeOutValue) timeOutValue.textContent = timing.checked_out_at || "—";
    resultHeading.textContent = state === "Present" || state === "Late" || state === "Checked Out" ? `${modeLabel()} recorded` : "Face match found";
    resultMessage.textContent = message || `Match distance: ${distance.toFixed(3)}. Confirming the face before saving.`;
    result.classList.remove("hidden");
  };

  const hideMatch = () => {
    result.classList.add("hidden");
    nextButton.classList.add("hidden");
  };

  const resetCameraPanel = () => {
    video.srcObject = null;
    video.classList.add("hidden");
    overlay.classList.add("hidden");
    indicator.classList.add("hidden");
    placeholder.classList.remove("hidden");
    startButton.classList.remove("hidden");
    stopButton.classList.add("hidden");
  };

  const stopAttendanceCamera = () => {
    monitor?.stop();
    monitor = null;
    stopStream(stream);
    stream = null;
    resetCameraPanel();
  };

  async function loadProfiles() {
    const response = await fetch(attendanceApp.dataset.profiles, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.message || "Registered face profiles could not be loaded.");
    if (!payload.profiles?.length) throw new Error("No registered face profiles are available. Register a student and capture one face first.");
    matchThreshold = Number(payload.threshold) || 0.48;
    profilesById = new Map(payload.profiles.map((student) => [String(student.id), student]));
    const labeled = payload.profiles.map((student) => new window.faceapi.LabeledFaceDescriptors(
      String(student.id), [new Float32Array(student.descriptor)]
    ));
    matcher = new window.faceapi.FaceMatcher(labeled, matchThreshold);
  }

  const finishAttendance = async (student, face, normalizedDescriptor, distance) => {
    const photo = cropFaceFromVideo(video, face);
    const actionLabel = modeLabel();
    if (!photo) {
      locked = false;
      setMessage("The face image could not be captured. Please scan again.", "error");
      return;
    }
    stopAttendanceCamera();
    status.textContent = "Saving…";
    resultHeading.textContent = `${actionLabel} face match confirmed`;
    resultMessage.textContent = `Saving automatic ${actionLabel}…`;
    try {
      const response = await fetch(attendanceApp.dataset.checkin, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          student_id: student.id,
          action: scanMode,
          face_descriptor: Array.from(normalizedDescriptor),
          face_photo_data: photo,
        }),
      });
      const payload = await response.json();
      const timing = { checked_in_at: payload.checked_in_at, checked_out_at: payload.checked_out_at };
      const displayState = payload.action === "time_out" && payload.ok ? "Checked Out" : (payload.status || "Not recorded");
      if (payload.student) showMatch(payload.student, distance, displayState, payload.message, timing);
      if (!response.ok || !payload.ok) throw new Error(payload.message || `${actionLabel} could not be saved.`);
      const recordedAt = scanMode === "time_out" ? payload.checked_out_at : payload.checked_in_at;
      showMatch(payload.student, distance, displayState, `${actionLabel} recorded for ${payload.student.full_name}: ${recordedAt}.`, timing);
      setMessage(payload.message, "success");
    } catch (error) {
      status.textContent = "Not recorded";
      resultHeading.textContent = `${actionLabel} not recorded`;
      resultMessage.textContent = error.message || `${actionLabel} could not be saved.`;
      setMessage(error.message || `${actionLabel} could not be saved.`, "error");
    } finally {
      nextButton.classList.remove("hidden");
    }
  };

  const processFaces = (faces, draw, detectorError) => {
    if (locked) return;
    if (detectorError) {
      stableId = null;
      stableFrames = 0;
      draw("unknown");
      setIndicator(indicator, "Face detection has a problem. Restart the scanner.", "error");
      return;
    }
    if (faces.length === 0) {
      stableId = null;
      stableFrames = 0;
      draw("unknown");
      setIndicator(indicator, "Look straight ahead at the camera.");
      setMessage("Waiting for a registered face…");
      return;
    }
    if (faces.length > 1) {
      stableId = null;
      stableFrames = 0;
      draw("unknown");
      setIndicator(indicator, "Only one student must be inside the camera frame.", "error");
      setMessage("More than one face was detected. Keep only one student in the frame.", "error");
      return;
    }
    if (!matcher) return;

    const face = faces[0];
    const normalizedDescriptor = normaliseDescriptor(face.descriptor);
    if (!normalizedDescriptor) {
      stableId = null;
      stableFrames = 0;
      draw("unknown");
      setIndicator(indicator, "Face data could not be read. Restart the scanner.", "error");
      return;
    }
    const match = matcher.findBestMatch(normalizedDescriptor);
    if (!match || match.label === "unknown") {
      stableId = null;
      stableFrames = 0;
      draw("unknown");
      setIndicator(indicator, "A face was found, but it does not match a registered profile.", "error");
      setMessage("The face could not be recognized. Improve the lighting or recapture the student profile.", "error");
      return;
    }

    const student = profilesById.get(String(match.label));
    if (!student) return;
    stableFrames = stableId === student.id ? stableFrames + 1 : 1;
    stableId = student.id;
    draw("matched");
    setIndicator(indicator, `Match found for ${student.full_name} — confirming…`, "ready");
    showMatch(student, match.distance, "Confirming match", "The scanner is briefly confirming the face before saving attendance.");
    setMessage(`${student.full_name} was recognized. Confirming the face match…`, "success");

    if (stableFrames >= STABLE_MATCH_FRAMES) {
      locked = true;
      finishAttendance(student, face, normalizedDescriptor, match.distance);
    }
  };

  const startAttendanceCamera = async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setMessage("This browser does not support camera access. Use the latest Chrome or Edge.", "error");
      return;
    }
    try {
      locked = false;
      stableId = null;
      stableFrames = 0;
      hideMatch();
      startButton.disabled = true;
      startButton.textContent = "Loading face profiles…";
      await loadFaceModels(attendanceApp.dataset.modelUrl);
      await loadProfiles();
      stream = await getCamera();
      video.srcObject = stream;
      await video.play();
      placeholder.classList.add("hidden");
      video.classList.remove("hidden");
      overlay.classList.remove("hidden");
      indicator.classList.remove("hidden");
      startButton.classList.add("hidden");
      stopButton.classList.remove("hidden");
      setIndicator(indicator, "Waiting for one face…");
      setMessage(`${profilesById.size} registered face profile${profilesById.size === 1 ? "" : "s"} loaded. ${modeLabel()} mode is ready.`);
      monitor = createRecognitionMonitor(video, overlay, processFaces);
      monitor.start();
    } catch (error) {
      stopAttendanceCamera();
      setMessage(error.message?.startsWith("Face") || error.message?.startsWith("No registered") ? error.message : cameraErrorText(error), "error");
    } finally {
      startButton.disabled = false;
      startButton.textContent = "◉ Start face scanner";
    }
  };

  modeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (locked) return;
      applyMode(button.dataset.mode);
      if (stream) setIndicator(indicator, `${modeLabel()} mode — waiting for one face.`);
    });
  });
  applyMode(scanMode);

  startButton?.addEventListener("click", startAttendanceCamera);
  stopButton?.addEventListener("click", () => {
    stopAttendanceCamera();
    stableId = null;
    stableFrames = 0;
    setMessage("Camera stopped.");
  });
  nextButton?.addEventListener("click", () => {
    locked = false;
    stableId = null;
    stableFrames = 0;
    hideMatch();
    setMessage("Ready to scan the next student.");
    startAttendanceCamera();
  });
  window.addEventListener("beforeunload", stopAttendanceCamera);
})();
