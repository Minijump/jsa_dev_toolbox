*** Test Cases ***
Create a contact
    Login As    admin
    Open Menu    Contacts
    Create Record
    Set Field    name    Robot Test Contact
    Set Field    Email    robot@example.com
    Save Record
    Field Should Be    Email    robot@example.com
