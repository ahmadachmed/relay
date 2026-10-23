*** Test Cases ***
Local Smoke Example
    [Tags]    smoke
    Should Be Equal As Integers    200    200

Intentional Failure Example
    [Tags]    failure-demo
    Should Be Equal    expired    accepted    Example only: expected OTP rejection.
