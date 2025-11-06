// Client-side upload progress using XMLHttpRequest (tracks upload to server)
const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('fileInput');
const progressBar = document.getElementById('uploadBar');
const progressWrap = document.querySelector('.progress');
const statusText = document.getElementById('statusText');
const submitBtn = document.getElementById('submitBtn');

if (form) {
  form.addEventListener('submit', function(e) {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) { alert('Please choose a ZIP file'); return; }

    const formData = new FormData();
    formData.append('file', file);

    submitBtn.disabled = true;
    progressWrap.style.display = 'block';
    statusText.textContent = 'Uploading to server...';

    const xhr = new XMLHttpRequest();
    xhr.open('POST', form.action, true);

    xhr.upload.addEventListener('progress', function(evt) {
      if (evt.lengthComputable) {
        const percent = Math.round((evt.loaded / evt.total) * 100);
        progressBar.style.width = percent + '%';
        progressBar.textContent = percent + '%';
      }
    });

    xhr.onload = function() {
      if (xhr.status >= 200 && xhr.status < 400) {
        // server responded with redirect to /success?upload_id=...
        document.open();
        document.write(xhr.responseText);
        document.close();
      } else {
        statusText.textContent = 'Upload failed: ' + xhr.status + ' ' + xhr.statusText;
        submitBtn.disabled = false;
      }
    };

    xhr.onerror = function() {
      statusText.textContent = 'Upload failed due to network error.';
      submitBtn.disabled = false;
    };

    xhr.send(formData);
  });
}
