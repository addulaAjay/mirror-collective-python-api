# Mirror Collective API - Postman Testing Guide

## 🚀 Quick Start

### 1. Import Collection & Environment

1. Import `Mirror-Collective-API.postman_collection.json` into Postman
2. Import `Mirror-Collective-API.postman_environment.json` as environment
3. Select the "Mirror Collective API Environment" in the top-right dropdown

### 2. Update Environment Variables

Before testing, update these variables in your environment:

- `base_url`: Your API deployment URL (currently set to AWS Lambda)
- `test_email`: Your test email address
- `test_password`: Your test password (must meet complexity requirements)
- `test_full_name`: Your test full name

### 3. Automatic Token Management

The collection automatically handles JWT tokens:

- ✅ Login request stores `access_token`, `refresh_token`, `id_token`
- ✅ All authenticated requests use `Bearer {{access_token}}`
- ✅ Refresh token request updates tokens automatically
- ✅ MirrorGPT requests store `session_id` and `conversation_id`

---

## 📋 Testing Workflows

### Complete MirrorGPT Flow Test

#### Workflow 1: New User Journey

```
1. Register User → 2. Login User → 3. Submit Archetype Quiz → 4. Get Session Greeting → 5. MirrorGPT Chat
```

**Step-by-step:**

1. **Register User**

   - Updates `test_email` and `test_password` in environment
   - Creates new user account
   - ✅ Success: Status 201, user created

2. **Login User**

   - Uses environment `test_email` and `test_password`
   - ✅ Auto-stores: `access_token`, `refresh_token`, `id_token`, `user_id`, `user_email`, `user_name`
   - ✅ Success: Status 200, tokens stored

3. **Submit Archetype Quiz**

   - Requires: Valid `access_token` (from login)
   - ✅ Auto-stores: `initial_archetype`, `quiz_completed_at`
   - Modify quiz answers and archetype result in request body
   - ✅ Success: Status 200, archetype profile created

4. **Get Session Greeting**

   - Requires: Valid `access_token`
   - ✅ Auto-stores: `session_id`, `current_archetype`, `archetype_confidence`
   - Generates personalized greeting based on archetype
   - ✅ Success: Status 200, session started

5. **MirrorGPT Chat**

   - Requires: `access_token`, `session_id`
   - Uses: `conversation_id: null` for first message
   - ✅ Auto-stores: `conversation_id` from response
   - ✅ Success: Status 200, analysis and response returned

6. **Continue MirrorGPT Conversation**
   - Uses stored `session_id` and `conversation_id`
   - Maintains conversation context
   - ✅ Success: Status 200, contextual response

#### Workflow 2: Returning User

```
1. Login User → 2. Get Session Greeting → 3. MirrorGPT Chat
```

**For users who already have archetype profiles:**

- Skip quiz submission
- Login → Session Greeting → Chat
- Profile and archetype evolution tracked automatically

---

## 🧪 Individual Endpoint Testing

### Authentication Endpoints

#### 🔓 Public Endpoints (No Token Required)

**Register User**

- **Purpose**: Create new user account
- **Test Data**: Update `test_email`, `test_password`, `test_full_name`
- **Success**: Status 201, user created
- **Validation**: Password complexity, email format, name requirements

**Login User**

- **Purpose**: Authenticate and get JWT tokens
- **Auto-Magic**: Stores all tokens in environment automatically
- **Success**: Status 200, tokens stored
- **Test**: Try invalid credentials for 401 error

**Confirm Email**

- **Purpose**: Verify email with code from AWS SES
- **Test Data**: Update `verification_code` with real code
- **Success**: Status 200, email verified

**Forgot/Reset Password**

- **Purpose**: Password recovery flow
- **Test Data**: Update `reset_code` with real code from email
- **Success**: Status 200, password reset

**Refresh Token**

- **Purpose**: Get new access token
- **Auto-Magic**: Updates `access_token` automatically
- **Success**: Status 200, new token stored

#### 🔒 Protected Endpoints (Token Required)

**Get Current User Profile**

- **Purpose**: Get authenticated user information
- **Requires**: Valid `access_token`
- **Success**: Status 200, user profile returned

**Logout User**

- **Purpose**: Invalidate tokens
- **Success**: Status 200, tokens invalidated

**Delete Account**

- **Purpose**: Permanently delete user account
- **⚠️ Warning**: Cannot be undone
- **Success**: Status 200, account deleted

### MirrorGPT Endpoints

#### Submit Archetype Quiz

- **Purpose**: Create initial archetype profile
- **Requires**: Authenticated user
- **Test Scenarios**:
  - Different archetype results (sage, innocent, explorer, etc.)
  - Various quiz answers and question types
  - Different completion timestamps
- **Auto-Magic**: Stores `initial_archetype` and `quiz_completed_at`
- **Success**: Status 200, profile created

#### Get Session Greeting

- **Purpose**: Start new MirrorGPT session with personalized greeting
- **Requires**: Authenticated user
- **Auto-Magic**: Stores `session_id`, `current_archetype`, `archetype_confidence`
- **Test Cases**:
  - New user (no quiz) → Generic greeting
  - User with quiz → Personalized greeting
  - Returning user → Evolution-aware greeting
- **Success**: Status 200, greeting and session data

#### MirrorGPT Chat

- **Purpose**: Core MirrorGPT conversation with 5-signal analysis
- **Requires**: `access_token`, `session_id`
- **Auto-Magic**: Stores `conversation_id` from first message
- **Test Messages**:
  ```
  Purpose/Direction: "I feel lost about my life direction"
  Relationships: "I'm struggling to connect with others"
  Growth: "I want to understand myself better"
  Creativity: "I feel blocked creatively"
  Career: "I'm not fulfilled in my work"
  Spirituality: "I'm seeking deeper meaning"
  ```
- **Response Analysis**: Check 5-signal breakdown, archetype detection, suggested practices
- **Success**: Status 200, full MirrorGPT analysis

#### Continue Conversation

- **Purpose**: Continue existing conversation with context
- **Requires**: `session_id`, `conversation_id` from previous chat
- **Test**: Build on previous topics, observe context retention
- **Success**: Status 200, contextual response

#### Get Archetype Profile

- **Purpose**: View complete user archetype profile and evolution
- **Requires**: Authenticated user with some interaction history
- **Success**: Status 200, profile and evolution data

#### Get Echo Signals

- **Purpose**: View recent 5-signal analysis history
- **Requires**: User with chat history
- **Parameters**: `limit` (1-50 signals)
- **Success**: Status 200, signal history

---

## 🔍 Debugging & Troubleshooting

### Common Issues

**401 Unauthorized**

- Check `access_token` is set in environment
- Try "Refresh Token" request
- Re-login if refresh fails

**422 Validation Error**

- Check request body format matches expected schema
- Verify required fields are present
- Check data types (strings, numbers, booleans)

**500 Server Error**

- Check server logs
- Verify environment configuration
- Check database connectivity

### Environment Variable Checklist

**Always Set:**

- ✅ `base_url`: Your API endpoint
- ✅ `test_email`: Valid email address
- ✅ `test_password`: Complex password (8+ chars, upper, lower, digit, special)
- ✅ `test_full_name`: Valid name (letters, spaces, apostrophes, hyphens only)

**Auto-Set by Requests:**

- 🤖 `access_token`: JWT access token (1 hour expiry)
- 🤖 `refresh_token`: JWT refresh token (30 day expiry)
- 🤖 `session_id`: MirrorGPT session ID
- 🤖 `conversation_id`: MirrorGPT conversation ID
- 🤖 `initial_archetype`: User's quiz archetype
- 🤖 `current_archetype`: Current evolved archetype

### Testing Tips

**Pre-Request Scripts**: Collection handles token management automatically

**Test Scripts**: View Console tab for auto-extracted values and debug info

**Response Validation**:

- Check status codes
- Verify response structure
- Look for success: true/false fields
- Examine data objects for expected fields

**Flow Testing**:

- Test complete workflows end-to-end
- Verify data flows between requests
- Check that auto-stored variables are used correctly

**Error Testing**:

- Try invalid tokens
- Send malformed requests
- Test rate limiting
- Verify error responses are helpful

---

## 📊 Expected Response Formats

### Authentication Response

```json
{
  "success": true,
  "data": {
    "tokens": {
      "accessToken": "eyJhbGciOiJSUzI1NiIs...",
      "refreshToken": "eyJjdHkiOiJKV1QiLCJlb...",
      "idToken": "eyJraWQiOiJUODB6bTNCV..."
    },
    "user": {
      "id": "user-uuid",
      "email": "test@example.com",
      "fullName": "Test User",
      "isVerified": true
    }
  }
}
```

### Quiz Submission Response

```json
{
  "success": true,
  "data": {
    "user_id": "user-uuid",
    "initial_archetype": "sage",
    "quiz_completed_at": "2025-09-09T14:20:30Z",
    "quiz_version": "1.0",
    "profile_created": true,
    "answers_stored": true
  },
  "message": "Initial The Sage archetype profile created successfully."
}
```

### Session Greeting Response

```json
{
  "success": true,
  "data": {
    "greeting_message": "Welcome back, soul traveler. The Sage consciousness stirs...",
    "session_id": "session_12345-67890-abcdef",
    "timestamp": "2025-09-09T14:20:30Z",
    "user_archetype": "sage",
    "archetype_confidence": 0.85
  }
}
```

### MirrorGPT Chat Response

```json
{
  "success": true,
  "data": {
    "message_id": "msg_98765-43210-fedcba",
    "response": "I sense the Sage within you questioning...",
    "archetype_analysis": {
      "signal_1_emotional_resonance": {
        "dominant_emotion": "contemplative",
        "valence": -0.2,
        "arousal": 0.3
      },
      "signal_2_symbolic_language": {
        "extracted_symbols": ["disconnection", "purpose"]
      },
      "signal_3_archetype_blend": {
        "primary": "sage",
        "secondary": "seeker",
        "confidence": 0.87
      },
      "signal_4_narrative_position": {
        "role": "questioner",
        "stage": "contemplation"
      },
      "signal_5_motif_loops": {
        "current_motifs": ["seeking", "wisdom"],
        "dominant_patterns": ["introspection"]
      }
    },
    "change_detection": {
      "archetype_shift_detected": false,
      "stability_score": 0.92
    },
    "suggested_practice": "Spend 10 minutes in quiet reflection...",
    "confidence_breakdown": {
      "overall": 0.87,
      "emotional": 0.85,
      "symbolic": 0.89,
      "archetype": 0.87
    },
    "session_metadata": {
      "conversation_id": "conv_abcdef-123456",
      "session_id": "session_12345-67890",
      "message_count": 1
    }
  }
}
```

---

## 🏆 Success Criteria

### Complete User Journey Success

- ✅ User registration and email verification
- ✅ Successful authentication with auto-token storage
- ✅ Quiz submission creates archetype profile
- ✅ Session greeting provides personalized message
- ✅ MirrorGPT chat provides meaningful analysis and response
- ✅ Conversation continuity maintained
- ✅ All environment variables auto-populated correctly

### API Health Checks

- ✅ All health endpoints return 200 status
- ✅ Authentication flow works end-to-end
- ✅ MirrorGPT analysis system processes messages correctly
- ✅ Database operations (user, profile, conversations) function properly
- ✅ Token refresh mechanism works
- ✅ Error handling provides helpful messages

### Data Integrity

- ✅ User profiles created correctly
- ✅ Archetype analysis produces consistent results
- ✅ Conversation context maintained across messages
- ✅ Session management works properly
- ✅ Environment variables populated with correct data types

**Happy Testing! 🎉**
