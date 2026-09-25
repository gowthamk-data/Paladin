import streamlit as st
from ui.utils.api_client import login, get_me, ask

st.set_page_config(page_title="Paladin", page_icon="🛡️", layout="wide")

if "token" not in st.session_state:
    st.session_state.token = None
if "user" not in st.session_state:
    st.session_state.user = None
if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("🛡️ Paladin - Secure Enterprise AI Gateway")

# ---------- LOGIN PAGE ----------
if st.session_state.token is None:
    st.subheader("Login")
    with st.form("login_form"):
        username = st.text_input("Username", placeholder="e.g. emp1")
        password = st.text_input("Password", type="password", placeholder="e.g. emp1_pass")
        submitted = st.form_submit_button("Login")
        if submitted:
            if not username or not password:
                st.error("Please enter both username and password.")
            else:
                result = login(username, password)
                if result and "access_token" in result:
                    st.session_state.token = result["access_token"]
                    user_info = get_me(st.session_state.token)
                    if user_info:
                        st.session_state.user = user_info
                        st.success(
                            f"Welcome, {user_info['username']}! "
                            f"(Role: {user_info['role']}, Clearance: {user_info['clearance_level']})"
                        )
                        st.rerun()
                    else:
                        st.error("Authentication failed — could not retrieve user info.")
                        st.session_state.token = None
                else:
                    st.error("Invalid username or password.")

# ---------- CHAT PAGE ----------
else:
    # Verify token is still valid
    user_info = get_me(st.session_state.token)
    if user_info is None:
        st.session_state.token = None
        st.session_state.user = None
        st.session_state.messages = []
        st.error("Session expired. Please log in again.")
        st.rerun()
    else:
        st.session_state.user = user_info

        # Header with user info and logout
        col1, col2 = st.columns([4, 1])
        with col1:
            st.info(
                f"Logged in as **{user_info['username']}** | "
                f"Role: {user_info['role']} | "
                f"Clearance: {user_info['clearance_level']}"
            )
        with col2:
            if st.button("Logout", key="logout_btn"):
                st.session_state.token = None
                st.session_state.user = None
                st.session_state.messages = []
                st.rerun()

        st.subheader("Ask Paladin")

        # Display chat history
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("sources"):
                    st.caption(f"Sources: {', '.join(msg['sources'])}")
                if msg.get("request_id"):
                    st.caption(f"Request ID: `{msg['request_id']}`")

        # Chat input
        if prompt := st.chat_input("Your question:"):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            response = ask(st.session_state.token, prompt)

            if response is None:
                with st.chat_message("assistant"):
                    st.error("No response from server.")
            elif response.get("error") == "UNAUTHORIZED":
                st.session_state.token = None
                st.session_state.user = None
                st.session_state.messages = []
                st.error("Session expired or invalid. Please log in again.")
                st.rerun()
            elif response.get("error"):
                with st.chat_message("assistant"):
                    st.error(f"Error: {response['error']}")
            elif "response" in response:
                with st.chat_message("assistant"):
                    st.markdown(response["response"])
                    sources = response.get("sources", [])
                    request_id = response.get("request_id", "")
                    if sources:
                        st.caption(f"Sources: {', '.join(sources)}")
                    if request_id:
                        st.caption(f"Request ID: `{request_id}`")
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": response["response"],
                    "sources": response.get("sources", []),
                    "request_id": response.get("request_id", ""),
                })
            else:
                with st.chat_message("assistant"):
                    st.error("Unexpected response format from server.")