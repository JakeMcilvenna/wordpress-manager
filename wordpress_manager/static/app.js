"use strict";
document.addEventListener("htmx:responseError", showRefreshError);
document.addEventListener("htmx:sendError", showRefreshError);
document.addEventListener("htmx:timeout", showRefreshError);
function showRefreshError() {
  const notice = document.getElementById("refresh-error");
  if (notice) notice.hidden = false;
}
document.addEventListener("htmx:afterSwap", () => {
  const notice = document.getElementById("refresh-error");
  if (notice) notice.hidden = true;
});
