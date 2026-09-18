// Add to admin.js, alongside your other tab handlers
document.getElementById("send-onboard-btn").addEventListener("click", async () => {
  const email = document.getElementById("onboard-email").value;
  const product_name = document.getElementById("onboard-product").value;
  const status = document.getElementById("onboard-status");

  status.textContent = "Sending...";
  try {
    const res = await fetch("/admin/send-onboarding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, product_name })
      // version/object_key auto-pulled server-side from current release info
    });
    const data = await res.json();
    status.textContent = res.ok ? "Sent ✅" : `Error: ${data.error}`;
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
});
