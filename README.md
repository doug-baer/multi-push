# multi-push
pushing files via SFTP using multiple streams per file

This is an experimental project to explore supporting multi-stream "push" of large files from a client to a server by breaking the source file into chunks and then uploading those chunks in parallel into the proper region within a file on the target machine.  Due to support for uploading to a specific offset within a file (supported since OpenSSH v9.0?), this should be possible. You can use seekable() in the Paramiko (https://www.paramiko.org/) library to see if it is supported for your connection.

Consider this a proof of concept and it sort of works but should not be used for production/critical work. I have built in some basic retry of the SSH connections and a rudimentary validation that the file size on the remote matches the size of the source, but that is no substitute for checking hashes -- though that takes significantly more time for big files. 

Small files give it some challenges as the progress bars do not know what to do and often do not show 100% even though the files have moved in a blink. The focus is large files, so I'll have to deal with that later. I tried to implement a "go to 100%" logic, but it may not do what I want. 

Example command line
```
./multi-push-sftp.py  --remote_host lvn-cat --username hol-mgr --directory_path /hol/lib/my-vpod   --remote_directory /hol/lib/my-vpod-copy
```

It uses key-based SSH, which must be setup in advance between the source and target -- and the source must be able to SSH into the target (port 22 is hardcoded into the script, but is a constant at the top and can easily be changed to match the environment.)

NOTE: By default, OpenSSH on Ubuntu can handle a significant number of simultaneous connections, but reaching, say, 100 concurrent connections will require adjustments to the configuration file (/etc/ssh/sshd_config) to ensure optimal performance and avoid potential issues with resource limitations on the server -- like exhaustion of available SSH connections. 

Configuration Parameter: The primary setting to control the maximum connections is called "MaxStartups" within the sshd_config file.
Default Value: "MaxStartups" is usually set to a lower number (around 10), which might not be sufficient for handling 100 simultaneous connections.

Recommendation: adjust this value to 100 -- or something that will accommodate the number of connections that you require. The number of connections is the number of simultaneous files multiplied by the number of simultaneous "chunks," with a buffer to account for connections starting before the others are completely closed and cleaned up. (This script is configured for 3 chunks per file and 2 concurrent files as I work on tweaking the server side.)

The MaxStartups parameter in OpenSSH specifies the maximum number of concurrent unauthenticated connections to the SSH daemon: 
Default value: 10:30:100 (or, in some versions, 10:30:60)

How it works: When the number of unauthenticated connections reaches the MaxStartups value, the server will drop new connection requests at a rate that increases linearly. For example, if the MaxStartups value is 10:30:100, the server will drop connection attempts with a 30% probability if there are 10 unauthenticated connections. 

How to configure: To configure MaxStartups, you can add or adjust the line MaxStartups in the /etc/ssh/sshd_config file. 


November 22, 2024