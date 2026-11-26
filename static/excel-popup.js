const excelForm = document.getElementById("open-excel-window");
const popupStatus = document.getElementById("excel-popup-status");

excelForm.addEventListener("submit", (event) => {
  const windowName = "finguardExcel" + Date.now();
  const excelWindow = window.open("", windowName, "popup,width=1120,height=850,resizable,scrollbars");
  if (!excelWindow) {
    event.preventDefault();
    popupStatus.textContent = "팝업을 허용한 뒤 엑셀 접수 폼 열기를 다시 눌러 주세요.";
    popupStatus.hidden = false;
    return;
  }
  excelForm.target = windowName;
  popupStatus.hidden = true;
});
