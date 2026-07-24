(() => {
  "use strict";

  const header = document.querySelector("[data-site-header]");
  const menuToggle = document.querySelector("[data-menu-toggle]");
  const siteNav = document.querySelector("[data-site-nav]");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const setMenuOpen = (open) => {
    if (!header || !menuToggle) return;
    header.classList.toggle("menu-open", open);
    menuToggle.setAttribute("aria-expanded", String(open));
  };

  const updateHeader = () => {
    if (!header) return;
    header.classList.toggle("is-scrolled", window.scrollY > 20);
  };

  let scrollFrame = 0;
  window.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(() => {
      updateHeader();
      scrollFrame = 0;
    });
  }, { passive: true });
  updateHeader();

  menuToggle?.addEventListener("click", () => {
    setMenuOpen(menuToggle.getAttribute("aria-expanded") !== "true");
  });

  siteNav?.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMenuOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setMenuOpen(false);
      menuToggle?.focus();
    }
  });

  document.addEventListener("click", (event) => {
    if (header?.classList.contains("menu-open") && !header.contains(event.target)) {
      setMenuOpen(false);
    }
  });

  const desktopQuery = window.matchMedia("(min-width: 861px)");
  desktopQuery.addEventListener?.("change", (event) => {
    if (event.matches) setMenuOpen(false);
  });

  const initializeOneShotMotion = ({
    targets,
    pendingClass,
    visibleClass,
    threshold,
    rootMargin,
  }) => {
    const elements = [...targets].filter(Boolean);
    let observer = null;

    const complete = (target) => {
      target.classList.remove(pendingClass);
      target.classList.add(visibleClass);
      observer?.unobserve(target);
    };

    const completeAll = () => {
      observer?.disconnect();
      observer = null;
      elements.forEach(complete);
    };

    if (!elements.length || reducedMotion.matches || !("IntersectionObserver" in window)) {
      completeAll();
      return completeAll;
    }

    elements.forEach((target) => target.classList.add(pendingClass));
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) complete(entry.target);
      });
    }, { threshold, rootMargin });
    elements.forEach((target) => observer.observe(target));

    return completeAll;
  };

  const completeWorkflowMotion = initializeOneShotMotion({
    targets: document.querySelectorAll("[data-workflow-sequence] > li"),
    pendingClass: "is-workflow-pending",
    visibleClass: "is-workflow-visible",
    threshold: 0.45,
    rootMargin: "0px 0px -8% 0px",
  });
  const completeSafetyMotion = initializeOneShotMotion({
    targets: [document.querySelector("[data-flow-sequence]")],
    pendingClass: "is-flow-pending",
    visibleClass: "is-flow-visible",
    threshold: 0.35,
    rootMargin: "0px 0px -8% 0px",
  });
  const completeTerminalMotion = initializeOneShotMotion({
    targets: [document.querySelector("[data-terminal-sequence]")],
    pendingClass: "is-terminal-pending",
    visibleClass: "is-terminal-visible",
    threshold: 0.35,
    rootMargin: "0px 0px -8% 0px",
  });

  const completeScrollMotion = () => {
    completeWorkflowMotion();
    completeSafetyMotion();
    completeTerminalMotion();
  };

  const writeClipboard = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }

    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  };

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const label = button.querySelector("span:not(.sr-only)");
    const status = button.querySelector("[data-copy-status]");
    const defaultLabel = label?.textContent || "Copy";
    let resetTimer = 0;

    button.addEventListener("click", async () => {
      const target = document.querySelector(button.dataset.copyTarget);
      if (!target) return;

      try {
        await writeClipboard(target.textContent.trim());
        button.classList.add("copied");
        if (label) label.textContent = "Copied";
        if (status) status.textContent = "Command copied to clipboard.";
      } catch {
        if (label) label.textContent = "Select text";
        if (status) status.textContent = "Copy failed. Select the command manually.";
        target.closest("pre")?.focus?.();
      }

      window.clearTimeout(resetTimer);
      resetTimer = window.setTimeout(() => {
        button.classList.remove("copied");
        if (label) label.textContent = defaultLabel;
        if (status) status.textContent = "";
      }, 1800);
    });
  });

  const demoVideo = document.querySelector("[data-demo-video]");
  const demoStatus = document.querySelector("[data-demo-status]");
  const demoPlay = document.querySelector("[data-demo-play]");
  const demoPlayLabel = document.querySelector("[data-demo-play-label]");
  const demoSound = document.querySelector("[data-demo-sound]");
  const demoSoundLabel = document.querySelector("[data-demo-sound-label]");
  const demoShell = demoVideo?.closest(".demo-video-shell");
  const demoTouchQuery = window.matchMedia("(hover: none) and (pointer: coarse)");
  const demoControlsIdleDelay = 1600;
  let demoInView = !("IntersectionObserver" in window);
  let demoPausedByUser = false;
  let demoControlsIdleTimer = 0;
  let demoVideoPointerRevealOnly = false;

  if (demoVideo && demoPlay && demoShell) {
    demoVideo.controls = false;
    demoShell.classList.add("has-custom-controls");
  }

  const resetDemoControlsIdleTimer = () => {
    window.clearTimeout(demoControlsIdleTimer);
    demoControlsIdleTimer = 0;
    demoShell?.classList.remove("is-controls-idle");

    if (!demoVideo || !demoShell || !demoTouchQuery.matches || demoVideo.paused) return;

    demoControlsIdleTimer = window.setTimeout(() => {
      if (!demoVideo.paused && demoTouchQuery.matches) {
        demoShell.classList.add("is-controls-idle");
      }
    }, demoControlsIdleDelay);
  };

  const updateDemoStatus = () => {
    if (!demoVideo || !demoStatus) return;
    const state = demoVideo.ended ? "finished" : demoVideo.paused ? "paused" : "playing";
    demoStatus.textContent = state === "finished" ? "Finished" : state === "paused" ? "Paused" : "Playing";
    demoStatus.closest(".demo-status")?.setAttribute("data-demo-state", state);
    demoShell?.classList.toggle("is-playing", state === "playing");
    resetDemoControlsIdleTimer();
    if (demoPlay) {
      const action = demoVideo.paused ? "Play" : "Pause";
      demoPlay.dataset.playing = String(!demoVideo.paused);
      demoPlay.setAttribute("aria-label", `${action} demonstration`);
      if (demoPlayLabel) demoPlayLabel.textContent = action;
    }
    if (demoSound) {
      const action = demoVideo.muted ? "Turn sound on" : "Mute";
      demoSound.dataset.muted = String(demoVideo.muted);
      demoSound.setAttribute("aria-label", action);
      if (demoSoundLabel) demoSoundLabel.textContent = action;
    }
  };

  const toggleDemoPlayback = () => {
    if (!demoVideo) return;
    if (demoVideo.paused) {
      demoPausedByUser = false;
      demoVideo.play().catch(updateDemoStatus);
    } else {
      demoPausedByUser = true;
      demoVideo.pause();
    }
  };

  const toggleDemoSound = () => {
    if (!demoVideo) return;
    demoVideo.muted = !demoVideo.muted;
    updateDemoStatus();
  };

  const handleDemoVideoClick = () => {
    if (demoVideoPointerRevealOnly) {
      demoVideoPointerRevealOnly = false;
      return;
    }

    if (demoTouchQuery.matches && demoShell?.classList.contains("is-controls-idle")) {
      resetDemoControlsIdleTimer();
      return;
    }

    toggleDemoPlayback();
  };

  const handleDemoPointerDown = (event) => {
    if (!demoTouchQuery.matches || !demoShell) return;
    demoVideoPointerRevealOnly =
      event.target === demoVideo && demoShell.classList.contains("is-controls-idle");
    resetDemoControlsIdleTimer();
  };

  const applyDemoMotionPreference = () => {
    if (!demoVideo) return;
    if (reducedMotion.matches || !demoInView) {
      demoVideo.pause();
      updateDemoStatus();
      return;
    }

    if (!demoPausedByUser) {
      demoVideo.play().catch(updateDemoStatus);
    } else {
      updateDemoStatus();
    }
  };

  if (demoVideo && "IntersectionObserver" in window) {
    const demoVisibilityObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        demoInView = entry.isIntersecting && entry.intersectionRatio >= 0.35;
        applyDemoMotionPreference();
      });
    }, {
      threshold: [0, 0.35, 1],
    });
    demoVisibilityObserver.observe(demoVideo);
  } else {
    applyDemoMotionPreference();
  }

  demoVideo?.addEventListener("play", updateDemoStatus);
  demoVideo?.addEventListener("pause", updateDemoStatus);
  demoVideo?.addEventListener("ended", updateDemoStatus);
  demoVideo?.addEventListener("click", handleDemoVideoClick);
  demoShell?.addEventListener("pointerdown", handleDemoPointerDown);
  demoShell?.addEventListener("pointercancel", () => {
    demoVideoPointerRevealOnly = false;
  });
  demoShell?.addEventListener("focusin", resetDemoControlsIdleTimer);
  demoPlay?.addEventListener("click", toggleDemoPlayback);
  demoSound?.addEventListener("click", toggleDemoSound);
  const handleMotionPreference = () => {
    if (reducedMotion.matches) completeScrollMotion();
    applyDemoMotionPreference();
  };

  if (typeof reducedMotion.addEventListener === "function") {
    reducedMotion.addEventListener("change", handleMotionPreference);
  } else {
    reducedMotion.addListener?.(handleMotionPreference);
  }

  if (typeof demoTouchQuery.addEventListener === "function") {
    demoTouchQuery.addEventListener("change", resetDemoControlsIdleTimer);
  } else {
    demoTouchQuery.addListener?.(resetDemoControlsIdleTimer);
  }
})();
