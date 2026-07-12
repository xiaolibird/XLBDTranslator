import io
from pathlib import Path

import streamlit as st

from web.services.backend_adapter import BackendAdapter


class MockUploadedFile:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self._buf = io.BytesIO(data)

    def getbuffer(self):
        return self._buf.getbuffer()


def test_build_settings_from_ui_creates_settings(tmp_path):
    # Prepare Streamlit session_state
    st.session_state.clear()
    st.session_state['api_token'] = 'AIzaFakeApiKey_for_test_1234567890'
    st.session_state['selected_persona'] = 'quality'
    st.session_state['use_vision'] = True
    st.session_state['pdf_boundaries'] = {}
    st.session_state['custom_prompt'] = {'mode': 'preset', 'mode_id': 'balanced'}
    st.session_state['selected_model'] = 'gemini-1.5-pro'

    # Create a mock uploaded file
    data = b"%PDF-1.7\n%fakepdf"
    mock_file = MockUploadedFile("sample.pdf", data)
    st.session_state['uploaded_files'] = [mock_file]

    adapter = BackendAdapter()

    settings = adapter.build_settings_from_ui()

    # If backend modules are not available, build_settings_from_ui returns None
    if settings is None:
        # assert that adapter detected backend unavailable
        assert not adapter.backend_available
        return

    # Validate settings values propagated from UI
    assert settings.api.gemini_api_key == st.session_state['api_token']
    # Selected model should be applied
    assert settings.api.gemini_model == st.session_state['selected_model']
    # Document path should point to a file with the uploaded filename
    assert settings.files.document_path.name == 'sample.pdf'
    # Vision mode should be enabled
    assert settings.processing.use_vision_mode is True or settings.processing.use_vision_mode == True
